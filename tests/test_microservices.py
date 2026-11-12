import sqlite3
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import closing

import httpx
import pytest

SERVICE_MODULES = {
    "auth": "services.auth.main:app",
    "accounts": "services.accounts.main:app",
    "transfers": "services.transfers.main:app",
    "transactions": "services.transactions.main:app",
    "assistant": "services.assistant.main:app",
    "gateway": "services.gateway.main:app",
}


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def banking_stack() -> Iterator[tuple[httpx.Client, list[subprocess.Popen[bytes]], str]]:
    ports = {service: available_port() for service in SERVICE_MODULES}
    urls = {service: f"http://127.0.0.1:{port}" for service, port in ports.items()}
    processes: list[subprocess.Popen[bytes]] = []
    with tempfile.TemporaryDirectory(prefix="harbor-services-") as data_dir:
        environment = os.environ.copy()
        environment.update(
            {
                "APP_ENV": "production",
                "SEED_DEMO_DATA": "true",
                "DATA_DIR": data_dir,
                "INTERNAL_API_KEY": "integration-test-key",
                "AUTH_SERVICE_URL": urls["auth"],
                "ACCOUNTS_SERVICE_URL": urls["accounts"],
                "TRANSFERS_SERVICE_URL": urls["transfers"],
                "TRANSACTIONS_SERVICE_URL": urls["transactions"],
                "ASSISTANT_SERVICE_URL": urls["assistant"],
                "PUBLIC_BASE_URL": urls["gateway"],
                "OPENAI_API_KEY": "",
                "DEMO_EMAIL": "demo@example.com",
                "DEMO_PASSWORD": "Demo123!",
            }
        )
        try:
            for name, module in SERVICE_MODULES.items():
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        module,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(ports[name]),
                    ],
                    cwd=os.path.dirname(os.path.dirname(__file__)),
                    env={
                        **environment,
                        **(
                            {
                                "MONGO_URL": "mongomock://localhost",
                                "MONGO_DATABASE": "harbor_transactions",
                            }
                            if name == "transactions"
                            else {
                                "DATABASE_URL": "sqlite:///"
                                + os.path.abspath(os.path.join(data_dir, f"{name}.db")).replace("\\", "/")
                            }
                        ),
                    },
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                processes.append(process)
                health_url = f"{urls[name]}/health"
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError(f"{name} exited with code {process.returncode} during startup.")
                    try:
                        with urllib.request.urlopen(health_url, timeout=0.5) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.1)
                else:
                    raise RuntimeError(f"{name} did not become healthy.")
            with httpx.Client(base_url=urls["gateway"], timeout=10.0) as client:
                yield client, processes, data_dir
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def create_account(client: httpx.Client, email: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "StrongPass123"},
    )
    assert response.status_code == 201
    return response.json()


def test_six_services_work_together_for_registration_deposit_transfer_and_search(
    banking_stack: tuple[httpx.Client, list[subprocess.Popen[bytes]], str],
) -> None:
    client, _processes, _data_dir = banking_stack
    sender = create_account(client, "sender@example.com")
    recipient = create_account(client, "recipient@example.com")
    sender_headers = {"Authorization": f"Bearer {sender['access_token']}"}
    recipient_headers = {"Authorization": f"Bearer {recipient['access_token']}"}

    assert client.get("/api/account", headers=sender_headers).json()["balance_cents"] == 0
    assert client.get("/api/account", headers=recipient_headers).json()["balance_cents"] == 0

    deposit_key = "deposit-test-0001"
    deposit = client.post(
        "/api/demo-deposits",
        headers={**sender_headers, "Idempotency-Key": deposit_key},
    )
    assert deposit.status_code == 201
    repeated_deposit = client.post(
        "/api/demo-deposits",
        headers={**sender_headers, "Idempotency-Key": deposit_key},
    )
    assert repeated_deposit.status_code == 201, repeated_deposit.text
    assert client.get("/api/account", headers=sender_headers).json()["balance_cents"] == 50_000
    other_user_key = client.post(
        "/api/demo-deposits",
        headers={**recipient_headers, "Idempotency-Key": deposit_key},
    )
    assert other_user_key.status_code == 409

    transfer_key = "transfer-test-0001"
    transfer = client.post(
        "/api/transfers",
        headers={**sender_headers, "Idempotency-Key": transfer_key},
        json={"recipient_email": "recipient@example.com", "amount": "12.34", "description": "Lunch"},
    )
    assert transfer.status_code == 201
    assert transfer.json()["amount_cents"] == 1234
    replay = client.post(
        "/api/transfers",
        headers={**sender_headers, "Idempotency-Key": transfer_key},
        json={"recipient_email": "recipient@example.com", "amount": "12.34", "description": "Lunch"},
    )
    assert replay.status_code == 201
    conflict = client.post(
        "/api/transfers",
        headers={**sender_headers, "Idempotency-Key": transfer_key},
        json={"recipient_email": "recipient@example.com", "amount": "20.00", "description": "Different"},
    )
    assert conflict.status_code == 409
    assert client.get("/api/account", headers=sender_headers).json()["balance_cents"] == 48_766
    assert client.get("/api/account", headers=recipient_headers).json()["balance_cents"] == 1234
    insufficient = client.post(
        "/api/transfers",
        headers={**sender_headers, "Idempotency-Key": "transfer-test-0002"},
        json={"recipient_email": "recipient@example.com", "amount": "500.00", "description": "Too much"},
    )
    assert insufficient.status_code == 400
    recipient_history = client.get("/api/transactions?q=lunch", headers=recipient_headers)
    assert recipient_history.status_code == 200
    assert len(recipient_history.json()["items"]) == 1
    assert recipient_history.json()["items"][0]["kind"] == "credit"

    history = client.get("/api/transactions?q=lunch", headers=sender_headers)
    assert history.status_code == 200
    assert len(history.json()["items"]) == 1
    assert history.json()["items"][0]["kind"] == "debit"
    assert client.get("/api/transactions").status_code == 401


def test_auth_assistant_and_seo_surfaces(
    banking_stack: tuple[httpx.Client, list[subprocess.Popen[bytes]], str],
) -> None:
    client, _processes, _data_dir = banking_stack
    login = client.post(
        "/api/auth/login",
        json={"email": "demo@example.com", "password": "Demo123!"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    chat = client.post(
        "/api/chat",
        headers=headers,
        json={"question": "How do I search transactions?"},
    )
    assert chat.status_code == 200
    assert "Transaction history" in chat.json()["answer"]
    assert client.post("/api/chat", json={"question": "hello"}).status_code == 401
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/account", headers=headers).status_code == 401
    assert client.get("/health").json()["service"] == "gateway"
    assert "Sitemap:" in client.get("/robots.txt").text
    assert "<urlset" in client.get("/sitemap.xml").text
    assert "http://127.0.0.1:" in client.get("/").text


def test_each_postgres_service_has_its_own_database(
    banking_stack: tuple[httpx.Client, list[subprocess.Popen[bytes]], str],
) -> None:
    client, _processes, data_dir = banking_stack
    expected_tables = {
        "gateway": "gateway_request_audit",
        "auth": "users",
        "accounts": "accounts",
        "transfers": "transfer_requests",
        "assistant": "chat_audit",
    }
    for service, table in expected_tables.items():
        database_path = os.path.join(data_dir, f"{service}.db")
        assert os.path.isfile(database_path)
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
        assert row is not None, f"{service} database is missing its {table} table"
    assert client.get("/health").json()["service"] == "gateway"
    assert client.get("/api/transactions").status_code == 401
