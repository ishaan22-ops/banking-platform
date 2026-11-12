import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.assistant import answer_question
from services.common import current_user


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)

    @field_validator("question")
    @classmethod
    def trim_question(cls, value: str) -> str:
        question = value.strip()
        if not question:
            raise ValueError("Enter a question.")
        return question


app = FastAPI(title="Harbor Assistant Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "assistant"}


@app.post("/api/chat")
async def chat(
    payload: ChatRequest,
    _user: dict[str, object] = Depends(current_user),
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
