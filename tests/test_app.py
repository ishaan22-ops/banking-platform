from fastapi.testclient import TestClient

from app.main import create_app


def create_client(seed_demo: bool = False) -> TestClient:
    app = create_app("sqlite:///:memory:", seed_demo=seed_demo)
    return TestClient(app)


def register(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "StrongPass123"},
    )
    assert response.status_code == 201
    return response.json()["access_token"]


def test_registration_auth_and_duplicate_email() -> None:
    with create_client() as client:
        token = register(client, "alice@example.com")
        account = client.get("/api/account", headers={"Authorization": f"Bearer {token}"})
        assert account.status_code == 200
        assert account.json()["balance_cents"] == 0

        duplicate = client.post(
            "/api/auth/register",
            json={"email": "ALICE@example.com", "password": "StrongPass123"},
        )
        assert duplicate.status_code == 409


def test_transfer_updates_both_balances_and_records_history() -> None:
    with create_client() as client:
        sender_token = register(client, "sender@example.com")
        recipient_token = register(client, "recipient@example.com")
        sender_headers = {"Authorization": f"Bearer {sender_token}"}
        recipient_headers = {"Authorization": f"Bearer {recipient_token}"}

        deposit = client.post("/api/demo-deposits", headers=sender_headers)
        assert deposit.status_code == 201
        response = client.post(
            "/api/transfers",
            headers=sender_headers,
            json={
                "recipient_email": "recipient@example.com",
                "amount": "12.34",
                "description": "Lunch",
            },
        )
        assert response.status_code == 201
        assert response.json()["amount_cents"] == 1234
        assert client.get("/api/account", headers=sender_headers).json()["balance_cents"] == 48_766
        assert client.get("/api/account", headers=recipient_headers).json()["balance_cents"] == 1234

        history = client.get("/api/transactions?q=lunch", headers=sender_headers)
        assert history.status_code == 200
        assert len(history.json()["items"]) == 1
        assert history.json()["items"][0]["kind"] == "debit"


def test_transfer_rejects_insufficient_funds_without_partial_credit() -> None:
    with create_client() as client:
        sender_token = register(client, "sender@example.com")
        recipient_token = register(client, "recipient@example.com")
        response = client.post(
            "/api/transfers",
            headers={"Authorization": f"Bearer {sender_token}"},
            json={"recipient_email": "recipient@example.com", "amount": "1.00"},
        )
        assert response.status_code == 400
        recipient = client.get(
            "/api/account",
            headers={"Authorization": f"Bearer {recipient_token}"},
        )
        assert recipient.json()["balance_cents"] == 0


def test_search_is_scoped_and_chatbot_has_local_fallback() -> None:
    with create_client() as client:
        token = register(client, "alice@example.com")
        response = client.post(
            "/api/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "How do I search transactions?"},
        )
        assert response.status_code == 200
        assert "Transaction history" in response.json()["answer"]

        assert client.get("/api/transactions").status_code == 401
        assert client.post("/api/chat", json={"question": "hello"}).status_code == 401


def test_seo_and_health_endpoints() -> None:
    with create_client() as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert "Sitemap:" in client.get("/robots.txt").text
        assert "<urlset" in client.get("/sitemap.xml").text
        assert "name=\"description\"" in client.get("/").text
        assert "http://localhost:8000/" in client.get("/").text
