import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AccessToken, User

PASSWORD_ITERATIONS = 310_000
TOKEN_LIFETIME_SECONDS = 12 * 60 * 60
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt_text}${digest_text}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, expected_text = stored_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        if iterations < 100_000 or iterations > 1_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
    except (ValueError, UnicodeError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_access_token(db: Session, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    db.add(
        AccessToken(
            token_hash=token_hash,
            user_id=user_id,
            expires_at=int(time.time()) + TOKEN_LIFETIME_SECONDS,
        )
    )
    return token


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to continue.")

    try:
        token_hash = hashlib.sha256(credentials.credentials.encode("ascii")).hexdigest()
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Your session is invalid.") from exc
    record = db.get(AccessToken, token_hash)
    if record is None or record.expires_at <= int(time.time()):
        if record is not None:
            db.delete(record)
            db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Your session has expired.")

    user = db.get(User, record.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Your session is invalid.")
    return user


def revoke_access_token(db: Session, credentials: HTTPAuthorizationCredentials | None) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        return
    try:
        token_hash = hashlib.sha256(credentials.credentials.encode("ascii")).hexdigest()
    except UnicodeEncodeError:
        return
    record = db.get(AccessToken, token_hash)
    if record is not None:
        db.delete(record)
        db.commit()
