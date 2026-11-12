import os
import time
from collections.abc import Generator

import httpx
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class ServiceBase(DeclarativeBase):
    pass


bearer_scheme = HTTPBearer(auto_error=False)


def build_database(service: str) -> tuple[Engine, sessionmaker[Session]]:
    path = os.getenv("DATA_DIR", "data")
    os.makedirs(path, exist_ok=True)
    database_path = os.path.abspath(os.path.join(path, f"{service}.db"))
    engine = create_engine(
        URL.create("sqlite", database=database_path),
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: object, _record: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db(request: Request) -> Generator[Session, None, None]:
    with request.app.state.session_factory() as db:
        yield db


def internal_key() -> str:
    key = os.getenv("INTERNAL_API_KEY")
    if key:
        return key
    if os.getenv("APP_ENV", "development").lower() == "production":
        raise RuntimeError("INTERNAL_API_KEY must be configured outside development.")
    return "local-development-key"


def require_internal_key(
    x_internal_key: str | None = Header(default=None),
) -> None:
    import hmac

    expected = internal_key()
    if not x_internal_key or not hmac.compare_digest(x_internal_key, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Internal service authentication required.")


async def call_service(
    base_url: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json: object | None = None,
    content: bytes | None = None,
    params: object | None = None,
) -> httpx.Response:
    service_headers = {"X-Internal-Key": internal_key()}
    if headers:
        service_headers.update(headers)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.request(
                method,
                f"{base_url.rstrip('/')}/{path.lstrip('/')}",
                headers=service_headers,
                json=json,
                content=content,
                params=params,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="A required banking service is temporarily unavailable.") from exc


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, object]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    auth_url = os.getenv("AUTH_SERVICE_URL", "http://auth:8000")
    response = await call_service(
        auth_url,
        "POST",
        "/internal/validate",
        headers={"Authorization": f"Bearer {credentials.credentials}"},
    )
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Your session is invalid or has expired.")
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="The authentication service is unavailable.")
    try:
        user = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="The authentication service returned an invalid response.") from exc
    if (
        not isinstance(user, dict)
        or not isinstance(user.get("id"), int)
        or not isinstance(user.get("email"), str)
    ):
        raise HTTPException(status_code=503, detail="The authentication service returned an invalid response.")
    return user


def token_expired(expires_at: int) -> bool:
    return expires_at <= int(time.time())
