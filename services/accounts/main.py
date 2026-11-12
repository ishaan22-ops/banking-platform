import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from services.common import (
    ServiceBase,
    build_database,
    call_service,
    current_user,
    get_db,
    require_internal_key,
)

engine, session_factory = build_database("accounts")
SEARCH_URL = os.getenv("TRANSACTIONS_SERVICE_URL", "http://transactions:8000")


def now() -> datetime:
    return datetime.now(timezone.utc)


class Account(ServiceBase):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    balance_cents: Mapped[int] = mapped_column(Integer, default=0)


class AccountOperation(ServiceBase):
    __tablename__ = "account_operations"

    idempotency_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    sender_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    recipient_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LedgerEntry(ServiceBase):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_account_created", "account_id", "created_at"),
        Index("ix_ledger_account_kind", "account_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class InternalTransfer(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)
    sender_id: int
    sender_email: str
    recipient_email: str = Field(min_length=3, max_length=254)
    amount_cents: int = Field(gt=0, le=100_000_000)
    description: str = Field(min_length=1, max_length=100)

    @field_validator("recipient_email", "sender_email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ServiceBase.metadata.create_all(engine)
    yield
    engine.dispose()


app = FastAPI(title="Harbor Accounts Service", lifespan=lifespan)
app.state.session_factory = session_factory


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "accounts"}


def get_or_create_account(db: Session, user: dict[str, object]) -> Account:
    account = db.scalar(select(Account).where(Account.user_id == user["id"]))
    if account is not None:
        return account
    account = Account(user_id=int(user["id"]), email=str(user["email"]), balance_cents=0)
    db.add(account)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        account = db.scalar(select(Account).where(Account.user_id == user["id"]))
        if account is None:
            raise
    return account


async def index_entry(entry: LedgerEntry, operation_key: str) -> None:
    response = await call_service(
        SEARCH_URL,
        "POST",
        "/internal/transactions",
        json={
            "id": entry.id,
            "operation_key": operation_key,
            "user_id": entry.user_id,
            "kind": entry.kind,
            "amount_cents": entry.amount_cents,
            "description": entry.description,
            "created_at": entry.created_at.isoformat(),
        },
    )
    if response.status_code not in (200, 201):
        raise HTTPException(status_code=503, detail="Transaction search is temporarily unavailable; retry safely.")


def ensure_same_transfer(
    db: Session,
    operation: AccountOperation,
    payload: InternalTransfer,
) -> None:
    sender = db.get(Account, operation.sender_id)
    recipient = db.get(Account, operation.recipient_id) if operation.recipient_id is not None else None
    if (
        operation.kind != "transfer"
        or sender is None
        or recipient is None
        or sender.user_id != payload.sender_id
        or sender.email != payload.sender_email
        or recipient.email != payload.recipient_email
        or operation.amount_cents != payload.amount_cents
        or operation.description != payload.description.strip()
    ):
        raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different transfer.")


@app.get("/api/account")
def account_summary(
    user: dict[str, object] = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    account = get_or_create_account(db, user)
    return {
        "email": account.email,
        "balance_cents": account.balance_cents,
        "currency": "USD",
        "account_id": account.id,
    }


@app.post("/api/demo-deposits", status_code=status.HTTP_201_CREATED)
async def demo_deposit(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict[str, object] = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if not idempotency_key or len(idempotency_key) < 8 or len(idempotency_key) > 100:
        raise HTTPException(status_code=400, detail="A valid Idempotency-Key header is required.")
    existing = db.get(AccountOperation, idempotency_key)
    if existing is None:
        account = get_or_create_account(db, user)
        operation = AccountOperation(
            idempotency_key=idempotency_key,
            kind="deposit",
            sender_id=account.id,
            amount_cents=50_000,
            description="Demo funds added",
        )
        db.add(operation)
        db.execute(
            update(Account)
            .where(Account.id == account.id)
            .values(balance_cents=Account.balance_cents + 50_000)
        )
        entry = LedgerEntry(
            id=f"{idempotency_key}:deposit",
            account_id=account.id,
            user_id=int(user["id"]),
            kind="credit",
            amount_cents=50_000,
            description="Demo funds added",
        )
        db.add(entry)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.get(AccountOperation, idempotency_key)
            if existing is None:
                raise
            account = db.get(Account, existing.sender_id)
            if (
                existing.kind != "deposit"
                or account is None
                or account.user_id != int(user["id"])
                or existing.amount_cents != 50_000
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used for a different action.",
                )
            entry = db.get(LedgerEntry, f"{idempotency_key}:deposit")
    else:
        account = db.get(Account, existing.sender_id)
        if (
            existing.kind != "deposit"
            or account is None
            or account.user_id != int(user["id"])
            or existing.amount_cents != 50_000
        ):
            raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different action.")
        entry = db.get(LedgerEntry, f"{idempotency_key}:deposit")
    if entry is None:
        raise HTTPException(status_code=500, detail="The deposit record could not be recovered.")
    await index_entry(entry, idempotency_key)
    return {"status": "completed", "amount_cents": 50_000, "currency": "USD"}


@app.post("/internal/transfers")
async def internal_transfer(
    payload: InternalTransfer,
    _internal: None = Depends(require_internal_key),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    existing = db.get(AccountOperation, payload.idempotency_key)
    if existing is not None:
        ensure_same_transfer(db, existing, payload)
    else:
        sender = get_or_create_account(
            db, {"id": payload.sender_id, "email": payload.sender_email}
        )
        recipient = db.scalar(select(Account).where(Account.email == payload.recipient_email))
        if sender.email == payload.recipient_email:
            raise HTTPException(status_code=400, detail="Transfers to your own account are not supported.")
        if recipient is None:
            raise HTTPException(status_code=404, detail="Recipient account not found.")
        debit = db.execute(
            update(Account)
            .where(Account.id == sender.id, Account.balance_cents >= payload.amount_cents)
            .values(balance_cents=Account.balance_cents - payload.amount_cents)
        )
        if debit.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=400, detail="Insufficient demo funds.")
        db.execute(
            update(Account)
            .where(Account.id == recipient.id)
            .values(balance_cents=Account.balance_cents + payload.amount_cents)
        )
        now_at = now()
        operation = AccountOperation(
            idempotency_key=payload.idempotency_key,
            kind="transfer",
            sender_id=sender.id,
            recipient_id=recipient.id,
            amount_cents=payload.amount_cents,
            description=payload.description.strip(),
            created_at=now_at,
        )
        debit_entry = LedgerEntry(
            id=f"{payload.idempotency_key}:debit",
            account_id=sender.id,
            user_id=payload.sender_id,
            kind="debit",
            amount_cents=payload.amount_cents,
            description=f"To {recipient.email}: {payload.description.strip()}",
            created_at=now_at,
        )
        credit_entry = LedgerEntry(
            id=f"{payload.idempotency_key}:credit",
            account_id=recipient.id,
            user_id=recipient.user_id,
            kind="credit",
            amount_cents=payload.amount_cents,
            description=f"From {sender.email}: {payload.description.strip()}",
            created_at=now_at,
        )
        db.add_all([operation, debit_entry, credit_entry])
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.get(AccountOperation, payload.idempotency_key)
            if existing is None:
                raise
            ensure_same_transfer(db, existing, payload)

    entries = [
        db.get(LedgerEntry, f"{payload.idempotency_key}:debit"),
        db.get(LedgerEntry, f"{payload.idempotency_key}:credit"),
    ]
    if any(entry is None for entry in entries):
        raise HTTPException(status_code=500, detail="The transfer record could not be recovered.")
    return {
        "status": "completed",
        "transaction_id": entries[0].id,
        "amount_cents": payload.amount_cents,
        "currency": "USD",
        "entries": [
            {
                "id": entry.id,
                "operation_key": payload.idempotency_key,
                "user_id": entry.user_id,
                "kind": entry.kind,
                "amount_cents": entry.amount_cents,
                "description": entry.description,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in entries
        ],
    }
