import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import DateTime, Integer, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from services.common import ServiceBase, build_database, call_service, current_user, get_db

ACCOUNTS_URL = os.getenv("ACCOUNTS_SERVICE_URL", "http://accounts:8000")
TRANSACTIONS_URL = os.getenv("TRANSACTIONS_SERVICE_URL", "http://transactions:8000")
engine, session_factory = build_database("transfers")


def now() -> datetime:
    return datetime.now(timezone.utc)


class TransferRecord(ServiceBase):
    __tablename__ = "transfer_requests"

    idempotency_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    sender_id: Mapped[int] = mapped_column(Integer, index=True)
    recipient_email: Mapped[str] = mapped_column(String(254))
    amount_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(100))
    transaction_id: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TransferRequest(BaseModel):
    recipient_email: str = Field(min_length=3, max_length=254)
    amount: Decimal
    description: str = Field(default="Demo transfer", min_length=1, max_length=100)

    @field_validator("recipient_email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized:
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ServiceBase.metadata.create_all(engine)
    yield
    engine.dispose()


app = FastAPI(title="Harbor Transfers Service", lifespan=lifespan)
app.state.session_factory = session_factory


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "transfers"}


@app.post("/api/transfers", status_code=status.HTTP_201_CREATED)
async def create_transfer(
    payload: TransferRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict[str, object] = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if not idempotency_key or len(idempotency_key) < 8 or len(idempotency_key) > 100:
        raise HTTPException(status_code=400, detail="A valid Idempotency-Key header is required.")
    existing = db.get(TransferRecord, idempotency_key)
    if existing is not None:
        if (
            existing.sender_id != int(user["id"])
            or existing.recipient_email != payload.recipient_email
            or existing.amount_cents != payload.amount_cents
            or existing.description != payload.description
        ):
            raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different transfer.")
        return {
            "status": "completed",
            "transaction_id": existing.transaction_id,
            "amount_cents": existing.amount_cents,
            "currency": "USD",
        }
    response = await call_service(
        ACCOUNTS_URL,
        "POST",
        "/internal/transfers",
        json={
            "idempotency_key": idempotency_key,
            "sender_id": user["id"],
            "sender_email": user["email"],
            "recipient_email": payload.recipient_email,
            "amount_cents": payload.amount_cents,
            "description": payload.description,
        },
    )
    if response.status_code != 200:
        try:
            body = response.json()
        except ValueError:
            body = {}
        detail = body.get("detail", "Transfer could not be completed.") if isinstance(body, dict) else "Transfer could not be completed."
        error_status = response.status_code if 400 <= response.status_code < 500 else 503
        raise HTTPException(status_code=error_status, detail=detail)

    try:
        result = response.json()
        entries = result["entries"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="The accounts service returned an invalid response.") from exc

    for entry in entries:
        indexed = await call_service(
            TRANSACTIONS_URL,
            "POST",
            "/internal/transactions",
            json=entry,
        )
        if indexed.status_code not in (200, 201):
            raise HTTPException(
                status_code=503,
                detail="Transfer completed. Search indexing is delayed; retry with the same Idempotency-Key.",
            )
    db.add(
        TransferRecord(
            idempotency_key=idempotency_key,
            sender_id=int(user["id"]),
            recipient_email=payload.recipient_email,
            amount_cents=payload.amount_cents,
            description=payload.description,
            transaction_id=str(result["transaction_id"]),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(TransferRecord, idempotency_key)
        if existing is None:
            raise HTTPException(status_code=503, detail="Transfer history could not be saved; retry safely.")
        if (
            existing.sender_id != int(user["id"])
            or existing.recipient_email != payload.recipient_email
            or existing.amount_cents != payload.amount_cents
            or existing.description != payload.description
        ):
            raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different transfer.")
    return {key: value for key, value in result.items() if key != "entries"}
