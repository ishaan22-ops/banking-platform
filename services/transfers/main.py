import os
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from services.common import call_service, current_user, require_internal_key

ACCOUNTS_URL = os.getenv("ACCOUNTS_SERVICE_URL", "http://accounts:8000")
TRANSACTIONS_URL = os.getenv("TRANSACTIONS_SERVICE_URL", "http://transactions:8000")


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


app = FastAPI(title="Harbor Transfers Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "transfers"}


@app.post("/api/transfers", status_code=status.HTTP_201_CREATED)
async def create_transfer(
    payload: TransferRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict[str, object] = Depends(current_user),
) -> dict[str, object]:
    if not idempotency_key or len(idempotency_key) < 8 or len(idempotency_key) > 100:
        raise HTTPException(status_code=400, detail="A valid Idempotency-Key header is required.")
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
    return {key: value for key, value in result.items() if key != "entries"}
