import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from services.common import ServiceBase, build_database, get_db, require_internal_key, token_expired

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_ITERATIONS = 310_000
TOKEN_LIFETIME_SECONDS = 12 * 60 * 60
engine, session_factory = build_database("auth")


class User(ServiceBase):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))


class AccessToken(ServiceBase):
    __tablename__ = "access_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(index=True)
    expires_at: Mapped[int] = mapped_column(index=True)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(normalized):
            raise ValueError("Enter a valid email address.")
        return normalized


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, rounds, salt, expected = stored_hash.split("$")
        if algorithm != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 1_000_000:
            return False
        calculated = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.urlsafe_b64decode(salt), int(rounds)
        )
        return hmac.compare_digest(calculated, base64.urlsafe_b64decode(expected))
    except (ValueError, UnicodeError):
        return False


def issue_token(db: Session, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        AccessToken(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            user_id=user_id,
            expires_at=int(time.time()) + TOKEN_LIFETIME_SECONDS,
        )
    )
    return token


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ServiceBase.metadata.create_all(engine)
    seed_demo = os.getenv(
        "SEED_DEMO_DATA",
        "true" if os.getenv("APP_ENV", "development").lower() == "development" else "false",
    )
    if seed_demo.lower() in {"true", "1", "yes"}:
        with session_factory() as db:
            demo_email = os.getenv("DEMO_EMAIL", "demo@example.com").strip().lower()
            demo_password = os.getenv("DEMO_PASSWORD", "Demo123!")
            if db.scalar(select(User).where(User.email == demo_email)) is None:
                db.add(User(email=demo_email, password_hash=hash_password(demo_password)))
                db.commit()
    yield
    engine.dispose()


app = FastAPI(title="Harbor Identity Service", lifespan=lifespan)
app.state.session_factory = session_factory


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "identity"}


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(payload: Credentials, db: Session = Depends(get_db)) -> dict[str, str]:
    if db.scalar(select(User).where(User.email == payload.email)) is not None:
        raise HTTPException(status_code=409, detail="An account with that email already exists.")
    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.flush()
        token = issue_token(db, user.id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with that email already exists.") from exc
    return {"access_token": token, "token_type": "bearer", "email": user.email}


@app.post("/api/auth/login")
def login(payload: Credentials, db: Session = Depends(get_db)) -> dict[str, str]:
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    token = issue_token(db, user.id)
    db.commit()
    return {"access_token": token, "token_type": "bearer", "email": user.email}


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    token = authorization[7:]
    try:
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=401, detail="Your session is invalid.") from exc
    access_token = db.get(AccessToken, token_hash)
    if access_token is None or token_expired(access_token.expires_at):
        raise HTTPException(status_code=401, detail="Your session is invalid or has expired.")
    db.delete(access_token)
    db.commit()


@app.post("/internal/validate", dependencies=[Depends(require_internal_key)])
def validate_token(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    token = authorization[7:]
    try:
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=401, detail="Your session is invalid.") from exc
    access_token = db.get(AccessToken, token_hash)
    if access_token is None or token_expired(access_token.expires_at):
        if access_token is not None:
            db.delete(access_token)
            db.commit()
        raise HTTPException(status_code=401, detail="Your session is invalid or has expired.")
    user = db.get(User, access_token.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Your session is invalid or has expired.")
    return {"id": user.id, "email": user.email}
