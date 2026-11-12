from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Index, Integer, String, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from services.common import ServiceBase, build_database, current_user, get_db, require_internal_key

engine, session_factory = build_database("transactions")


class SearchTransaction(ServiceBase):
    __tablename__ = "transaction_search"
    __table_args__ = (
        Index("ix_transaction_search_user_created", "user_id", "created_at"),
        Index("ix_transaction_search_user_kind", "user_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    operation_key: Mapped[str] = mapped_column(String(100), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class IndexRequest(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    operation_key: str = Field(min_length=8, max_length=100)
    user_id: int = Field(gt=0)
    kind: str = Field(pattern="^(credit|debit)$")
    amount_cents: int = Field(gt=0)
    description: str = Field(min_length=1, max_length=200)
    created_at: datetime


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ServiceBase.metadata.create_all(engine)
    yield
    engine.dispose()


app = FastAPI(title="Harbor Transaction Search Service", lifespan=lifespan)
app.state.session_factory = session_factory


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "transactions"}


@app.post("/internal/transactions", dependencies=[Depends(require_internal_key)])
def index_transaction(payload: IndexRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    existing = db.get(SearchTransaction, payload.id)
    if existing is None:
        db.add(SearchTransaction(**payload.model_dump()))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.get(SearchTransaction, payload.id)
            if existing is None:
                raise
    return {"status": "indexed"}


@app.get("/api/transactions")
def search_transactions(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10000),
    user: dict[str, object] = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    query = select(SearchTransaction).where(SearchTransaction.user_id == user["id"])
    search_text = q.strip()
    if search_text:
        pattern = f"%{search_text}%"
        query = query.where(
            or_(
                SearchTransaction.description.ilike(pattern),
                SearchTransaction.kind.ilike(pattern),
            )
        )
    entries = list(
        db.scalars(
            query.order_by(SearchTransaction.created_at.desc(), SearchTransaction.id.desc())
            .limit(limit + 1)
            .offset(offset)
        )
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
