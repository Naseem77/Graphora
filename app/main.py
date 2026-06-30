from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from app.github.webhooks import dispatch_webhook, verify_signature
from app.state import init_state, list_graph_builds


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_state()
    yield


app = FastAPI(title="GraphReview Bot", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status")
async def status() -> dict[str, list]:
    try:
        builds = list_graph_builds()
    except Exception as exc:  # noqa: BLE001 - dashboard must not crash on DB issues
        logger.warning("Failed to read graph build status: %s", exc)
        builds = []
    return {"builds": builds}


@app.get("/dashboard")
async def dashboard() -> FileResponse:
    return FileResponse(_STATIC_DIR / "dashboard.html")


@app.post("/webhook")
async def webhook(
    background_tasks: BackgroundTasks,
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> dict[str, bool]:
    body = await request.body()
    if not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    await dispatch_webhook(x_github_event, payload, background_tasks, x_github_delivery)
    return {"ok": True}
