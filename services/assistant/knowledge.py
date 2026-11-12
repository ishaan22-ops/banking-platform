import os

import httpx


def local_answer(question: str) -> str:
    text = question.casefold()
    if any(word in text for word in ("transfer", "send money", "pay")):
        return "To make a demo transfer, sign in and use the Transfer form with the recipient's registered email. Transfers only move simulated funds within this demo."
    if any(word in text for word in ("balance", "money", "funds")):
        return "Your simulated balance is shown on the dashboard after you sign in. This app does not connect to a real bank."
    if any(word in text for word in ("search", "transaction", "history")):
        return "Open Transaction history and enter a word such as a description or transfer type. Results are limited to your own account."
    if any(word in text for word in ("password", "login", "sign in", "account")):
        return "You can create a demo account from the sign-in page. Never share passwords, PINs, card details, or account numbers in chat."
    return "I can help with demo transfers, balances, transaction search, and sign-in. Please don't share passwords, PINs, card details, or other sensitive information."


async def answer_question(question: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return local_answer(question)

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a concise support assistant for a simulated banking demo. "
                    "Explain demo features only. Never claim to perform transactions or access account data. "
                    "Tell users not to share passwords, PINs, card details, or account numbers."
                ),
            },
            {"role": "user", "content": question},
        ],
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]
