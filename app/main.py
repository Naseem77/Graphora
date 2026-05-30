from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException, Request

from app.github.webhooks import dispatch_webhook, verify_signature


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="GraphReview Bot", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, bool]:
    body = await request.body()
    if not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    await dispatch_webhook(x_github_event, payload)
    return {"ok": True}
