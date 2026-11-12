import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from services.common import current_user, require_internal_key

MONGO_URL = os.getenv("MONGO_URL")
MONGO_DATABASE = os.getenv("MONGO_DATABASE", "harbor_transactions")
client: Any
collection: Any


def to_epoch_milliseconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    utc_value = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return int((utc_value - epoch) // timedelta(milliseconds=1))


class IndexRequest(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    operation_key: str = Field(min_length=8, max_length=100)
    user_id: int = Field(gt=0)
    kind: str = Field(pattern="^(credit|debit)$")
    amount_cents: int = Field(gt=0)
    description: str = Field(min_length=1, max_length=200)
    created_at: datetime


def create_client() -> Any:
    if MONGO_URL and MONGO_URL.startswith("mongomock://"):
        import mongomock

        return mongomock.MongoClient(tz_aware=True)
    if "MONGO_HOST" in os.environ:
        return MongoClient(
            host=os.environ["MONGO_HOST"],
            port=int(os.getenv("MONGO_PORT", "27017")),
            username=os.getenv("MONGO_USER"),
            password=os.getenv("MONGO_PASSWORD"),
            authSource=MONGO_DATABASE,
            serverSelectionTimeoutMS=5000,
            tz_aware=True,
        )
    if not MONGO_URL:
        if os.getenv("APP_ENV", "development").lower() == "production":
            raise RuntimeError("Configure MONGO_HOST or MONGO_URL for the transactions service.")
        return MongoClient(
            host="localhost",
            port=27017,
            username="harbor_transactions",
            password="harbor-mongo-app-local",
            authSource=MONGO_DATABASE,
            serverSelectionTimeoutMS=5000,
            tz_aware=True,
        )
    return MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000, tz_aware=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global client, collection
    client = create_client()
    client.admin.command("ping")
    collection = client[MONGO_DATABASE]["transactions"]
    collection.create_index([("user_id", 1), ("created_at", -1)])
    collection.create_index([("user_id", 1), ("kind", 1)])
    yield
    client.close()


app = FastAPI(title="Harbor MongoDB Transaction Search Service", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    client.admin.command("ping")
    return {"status": "ok", "service": "transactions", "database": "mongodb"}


@app.post("/internal/transactions", dependencies=[Depends(require_internal_key)])
def index_transaction(payload: IndexRequest) -> dict[str, str]:
    document = payload.model_dump()
    document["_id"] = document.pop("id")
    try:
        collection.insert_one(document)
    except DuplicateKeyError:
        existing = collection.find_one({"_id": document["_id"]})
        mismatches = [
            field
            for field in ("operation_key", "user_id", "kind", "amount_cents", "description")
            if existing is None or existing.get(field) != document[field]
        ]
        if existing is None:
            mismatches.append("missing")
        elif (
            to_epoch_milliseconds(existing["created_at"])
            != to_epoch_milliseconds(document["created_at"])
        ):
            mismatches.append("created_at")
        if mismatches:
            raise HTTPException(
                status_code=409,
                detail=f"Transaction ID was already used for a different entry ({', '.join(mismatches)}).",
            )
    return {"status": "indexed"}


@app.get("/api/transactions")
def search_transactions(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10000),
    user: dict[str, object] = Depends(current_user),
) -> dict[str, object]:
    query: dict[str, object] = {"user_id": user["id"]}
    search_text = q.strip()
    if search_text:
        pattern = re.escape(search_text)
        query["$or"] = [
            {"description": {"$regex": pattern, "$options": "i"}},
            {"kind": {"$regex": pattern, "$options": "i"}},
        ]

    cursor = collection.find(query).sort([("created_at", -1), ("_id", -1)]).skip(offset).limit(limit + 1)
    documents = list(cursor)
    has_more = len(documents) > limit
    documents = documents[:limit]
    return {
        "items": [
            {
                "id": document["_id"],
                "kind": document["kind"],
                "amount_cents": document["amount_cents"],
                "description": document["description"],
                "created_at": document["created_at"].isoformat(),
            }
            for document in documents
        ],
        "has_more": has_more,
        "limit": limit,
        "offset": offset,
    }
