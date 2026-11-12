import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from services.common import (
    ServiceBase,
    build_database,
    call_service,
    current_user,
    database_ready,
    get_db,
    require_internal_key,
)

engine, session_factory = build_database("accounts")
SEARCH_URL = os.getenv("TRANSACTIONS_SERVICE_URL", "http://transactions:8000")
logger = logging.getLogger(__name__)


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


class TransactionOutbox(ServiceBase):
    __tablename__ = "transaction_outbox"

    entry_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(100))
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    publisher = asyncio.create_task(publish_transaction_outbox())
    try:
        yield
    finally:
        publisher.cancel()
        try:
            await publisher
        except asyncio.CancelledError:
            pass
        engine.dispose()


app = FastAPI(title="Harbor Accounts Service", lifespan=lifespan)
app.state.session_factory = session_factory


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "accounts"}


@app.get("/ready")
def ready() -> dict[str, str]:
    return database_ready(engine, "accounts")


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


def queue_transaction_index(
    db: Session,
    entry: LedgerEntry,
    operation_key: str,
) -> None:
    db.add(
        TransactionOutbox(
            entry_id=entry.id,
            operation_key=operation_key,
            user_id=entry.user_id,
            kind=entry.kind,
            amount_cents=entry.amount_cents,
            description=entry.description,
            created_at=entry.created_at,
        )
    )


def load_pending_outbox_events() -> list[dict[str, object]]:
    with session_factory() as db:
        pending = list(
            db.scalars(
                select(TransactionOutbox)
                .where(
                    TransactionOutbox.published_at.is_(None),
                    TransactionOutbox.next_attempt_at <= now(),
                )
                .order_by(TransactionOutbox.created_at, TransactionOutbox.entry_id)
                .limit(25)
            )
        )
        return [
            {
                "entry_id": event.entry_id,
                "operation_key": event.operation_key,
                "user_id": event.user_id,
                "kind": event.kind,
                "amount_cents": event.amount_cents,
                "description": event.description,
                "created_at": event.created_at.isoformat(),
            }
            for event in pending
        ]


def update_outbox_attempt(entry_id: str, *, published: bool) -> None:
    with session_factory() as db:
        record = db.get(TransactionOutbox, entry_id)
        if record is None or record.published_at is not None:
            return
        if published:
            record.published_at = now()
        else:
            record.attempts += 1
            retry_seconds = min(2 ** min(record.attempts, 8), 300)
            record.next_attempt_at = now() + timedelta(seconds=retry_seconds)
        db.commit()


async def publish_transaction_outbox() -> None:
    while True:
        try:
            events = await asyncio.to_thread(load_pending_outbox_events)
            for event in events:
                entry_id = str(event["entry_id"])
                try:
                    response = await call_service(
                        SEARCH_URL,
                        "POST",
                        "/internal/transactions",
                        json={
                            "id": event["entry_id"],
                            **{key: value for key, value in event.items() if key != "entry_id"},
                        },
                    )
                except HTTPException as exc:
                    logger.warning(
                        "Transaction index delivery failed for %s: %s",
                        entry_id,
                        exc.detail,
                    )
                    await asyncio.to_thread(update_outbox_attempt, entry_id, published=False)
                    continue
                if response.status_code not in (200, 201):
                    logger.warning(
                        "Transaction index rejected entry %s with status %s",
                        entry_id,
                        response.status_code,
                    )
                    await asyncio.to_thread(update_outbox_attempt, entry_id, published=False)
                    continue
                await asyncio.to_thread(update_outbox_attempt, entry_id, published=True)
        except SQLAlchemyError:
            logger.exception("Transaction outbox database operation failed.")
        await asyncio.sleep(1)


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


@app.get("/api/account/ledger")
def account_ledger(
    kind: str | None = Query(default=None, pattern="^(credit|debit)$"),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10000),
    user: dict[str, object] = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    account = get_or_create_account(db, user)
    statement = select(LedgerEntry).where(LedgerEntry.account_id == account.id)
    if kind is not None:
        statement = statement.where(LedgerEntry.kind == kind)
    entries = list(
        db.scalars(
            statement.order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
    )
    has_more = len(entries) > limit
    return {
        "items": [
            {
                "id": entry.id,
                "kind": entry.kind,
                "amount_cents": entry.amount_cents,
                "description": entry.description,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in entries[:limit]
        ],
        "has_more": has_more,
        "limit": limit,
        "offset": offset,
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
        db.flush()
        queue_transaction_index(db, entry, idempotency_key)
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
        db.flush()
        queue_transaction_index(db, debit_entry, payload.idempotency_key)
        queue_transaction_index(db, credit_entry, payload.idempotency_key)
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
