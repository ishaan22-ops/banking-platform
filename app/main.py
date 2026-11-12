import html
import os
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, PlainTextResponse, Response as FastAPIResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.assistant import answer_question
from app.database import Base, build_engine, build_session_factory, default_database_url, get_db
from app.models import Account, Transaction, User
from app.security import (
    bearer_scheme,
    create_access_token,
    get_current_user,
    hash_password,
    revoke_access_token,
    verify_password,
)

STATIC_DIR = Path(__file__).parent / "static"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(normalized):
            raise ValueError("Enter a valid email address.")
        return normalized


class TransferRequest(BaseModel):
    recipient_email: str = Field(min_length=3, max_length=254)
    amount: Decimal
    description: str = Field(default="Demo transfer", min_length=1, max_length=100)

    @field_validator("recipient_email")
    @classmethod
    def normalize_recipient_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(normalized):
            raise ValueError("Enter a valid recipient email address.")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Enter a transfer note.")
        return normalized

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0 or value.as_tuple().exponent < -2:
            raise ValueError("Amount must be positive and have no more than two decimal places.")
        if value > Decimal("1000000"):
            raise ValueError("Amount exceeds the demo transfer limit.")
        return value

    @property
    def amount_cents(self) -> int:
        return int(self.amount * 100)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)

    @field_validator("question")
    @classmethod
    def trim_question(cls, value: str) -> str:
        question = value.strip()
        if not question:
            raise ValueError("Enter a question.")
        return question


def create_app(database_url: str | None = None, seed_demo: bool | None = None) -> FastAPI:
    resolved_url = database_url or default_database_url()
    engine = build_engine(resolved_url)
    session_factory = build_session_factory(engine)
    should_seed = seed_demo if seed_demo is not None else (
        os.getenv("SEED_DEMO_DATA", "true" if os.getenv("APP_ENV", "development") == "development" else "false")
        .strip()
        .lower()
        in {"true", "1", "yes"}
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        if should_seed:
            with session_factory() as db:
                seed_demo_user(db)
        yield
        engine.dispose()

    app = FastAPI(
        title="Harbor Demo Banking",
        description="A simulated banking application for container and Kubernetes demonstrations.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", include_in_schema=False)
    def home() -> HTMLResponse:
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        canonical_url = public_base_url()
        return HTMLResponse(page.replace("__PUBLIC_BASE_URL__", html.escape(canonical_url, quote=True)))

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        base_url = public_base_url()
        return PlainTextResponse(
            f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /health\nSitemap: {base_url}/sitemap.xml\n"
        )

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap() -> FastAPIResponse:
        base_url = public_base_url()
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>{xml_escape(base_url)}/</loc></url></urlset>"
        )
        return FastAPIResponse(content=body, media_type="application/xml")

    @app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
    def register(payload: Credentials, db: Session = Depends(get_db)) -> dict[str, object]:
        user = User(email=payload.email, password_hash=hash_password(payload.password))
        db.add(user)
        try:
            db.flush()
            account = Account(user_id=user.id, balance_cents=0)
            db.add(account)
            token = create_access_token(db, user.id)
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
        token = create_access_token(db, user.id)
        db.commit()
        return {"access_token": token, "token_type": "bearer", "email": user.email}

    @app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(
        credentials=Depends(bearer_scheme),
        db: Session = Depends(get_db),
        _user: User = Depends(get_current_user),
    ) -> Response:
        revoke_access_token(db, credentials)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/account")
    def account_summary(
        user: User = Depends(get_current_user), db: Session = Depends(get_db)
    ) -> dict[str, object]:
        account = db.scalar(select(Account).where(Account.user_id == user.id))
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found.")
        return {
            "email": user.email,
            "balance_cents": account.balance_cents,
            "currency": "USD",
            "account_id": account.id,
        }

    @app.post("/api/demo-deposits", status_code=status.HTTP_201_CREATED)
    def add_demo_funds(
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        account = db.scalar(select(Account).where(Account.user_id == user.id))
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found.")
        amount_cents = 50_000
        db.execute(
            update(Account)
            .where(Account.id == account.id)
            .values(balance_cents=Account.balance_cents + amount_cents)
        )
        db.add(
            Transaction(
                account_id=account.id,
                kind="credit",
                amount_cents=amount_cents,
                description="Demo funds added",
            )
        )
        db.commit()
        return {"status": "completed", "amount_cents": amount_cents, "currency": "USD"}

    @app.post("/api/transfers", status_code=status.HTTP_201_CREATED)
    def create_transfer(
        payload: TransferRequest,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        if payload.recipient_email == user.email:
            raise HTTPException(status_code=400, detail="Transfers to your own account are not supported.")

        sender = db.scalar(select(Account).where(Account.user_id == user.id))
        recipient = db.scalar(
            select(Account).join(User, Account.user_id == User.id).where(User.email == payload.recipient_email)
        )
        if sender is None:
            raise HTTPException(status_code=404, detail="Account not found.")
        if recipient is None:
            raise HTTPException(status_code=404, detail="Recipient account not found.")

        amount_cents = payload.amount_cents
        debit = db.execute(
            update(Account)
            .where(Account.id == sender.id, Account.balance_cents >= amount_cents)
            .values(balance_cents=Account.balance_cents - amount_cents)
        )
        if debit.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=400, detail="Insufficient demo funds.")
        db.execute(
            update(Account)
            .where(Account.id == recipient.id)
            .values(balance_cents=Account.balance_cents + amount_cents)
        )
        debit_entry = Transaction(
            account_id=sender.id,
            kind="debit",
            amount_cents=amount_cents,
            description=f"To {payload.recipient_email}: {payload.description}",
        )
        credit_entry = Transaction(
            account_id=recipient.id,
            kind="credit",
            amount_cents=amount_cents,
            description=f"From {user.email}: {payload.description}",
        )
        db.add_all([debit_entry, credit_entry])
        db.commit()
        db.refresh(debit_entry)
        return {
            "status": "completed",
            "transaction_id": debit_entry.id,
            "amount_cents": amount_cents,
            "currency": "USD",
        }

    @app.get("/api/transactions")
    def list_transactions(
        q: Annotated[str, Query(max_length=100)] = "",
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        account = db.scalar(select(Account).where(Account.user_id == user.id))
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found.")

        query = select(Transaction).where(Transaction.account_id == account.id)
        search_text = q.strip()
        if search_text:
            pattern = f"%{search_text}%"
            query = query.where(
                or_(
                    Transaction.description.ilike(pattern),
                    Transaction.kind.ilike(pattern),
                )
            )
        entries = list(
            db.scalars(query.order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(limit + 1).offset(offset))
        )
        has_more = len(entries) > limit
        entries = entries[:limit]
        return {
            "items": [
                {
                    "id": entry.id,
                    "kind": entry.kind,
                    "amount_cents": entry.amount_cents,
                    "description": entry.description,
                    "created_at": entry.created_at.isoformat(),
                }
                for entry in entries
            ],
            "has_more": has_more,
            "limit": limit,
            "offset": offset,
        }

    @app.post("/api/chat")
    async def chat(
        payload: ChatRequest,
        _user: User = Depends(get_current_user),
    ) -> dict[str, str]:
        try:
            answer = await answer_question(payload.question)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail="The AI assistant is temporarily unavailable. Please try again.",
            ) from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail="The AI assistant returned an invalid response. Please try again.",
            ) from exc
        if not isinstance(answer, str) or not answer.strip():
            raise HTTPException(status_code=502, detail="The AI assistant returned an empty response.")
        return {"answer": answer.strip()}

    return app


def seed_demo_user(db: Session) -> None:
    email = os.getenv("DEMO_EMAIL", "demo@example.com").strip().lower()
    password = os.getenv("DEMO_PASSWORD", "Demo123!")
    if not EMAIL_PATTERN.fullmatch(email):
        raise RuntimeError("DEMO_EMAIL must be a valid email address.")
    if len(password) < 8:
        raise RuntimeError("DEMO_PASSWORD must contain at least 8 characters.")

    if db.scalar(select(User).where(User.email == email)) is not None:
        return
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.flush()
    account = Account(user_id=user.id, balance_cents=250_000)
    db.add(account)
    db.flush()
    db.add(
        Transaction(
            account_id=account.id,
            kind="credit",
            amount_cents=250_000,
            description="Demo opening balance",
        )
    )
    db.commit()


def public_base_url() -> str:
    value = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("PUBLIC_BASE_URL must be an absolute http or https origin without credentials or a path.")
    return value


app = create_app()
