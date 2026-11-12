from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import DateTime, Integer
from sqlalchemy.orm import Mapped, Session, mapped_column

from services.assistant.knowledge import answer_question
from services.common import ServiceBase, build_database, current_user, database_ready, get_db

engine, session_factory = build_database("assistant")


class ChatAudit(ServiceBase):
    __tablename__ = "chat_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    question_length: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)

    @field_validator("question")
    @classmethod
    def trim_question(cls, value: str) -> str:
        question = value.strip()
        if not question:
            raise ValueError("Enter a question.")
        return question


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ServiceBase.metadata.create_all(engine)
    yield
    engine.dispose()


app = FastAPI(title="Harbor Assistant Service", lifespan=lifespan)
app.state.session_factory = session_factory


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "assistant"}


@app.get("/ready")
def ready() -> dict[str, str]:
    return database_ready(engine, "assistant")


@app.post("/api/chat")
async def chat(
    payload: ChatRequest,
    user: dict[str, object] = Depends(current_user),
    db: Session = Depends(get_db),
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
    db.add(ChatAudit(user_id=int(user["id"]), question_length=len(payload.question)))
    db.commit()
    return {"answer": answer.strip()}
