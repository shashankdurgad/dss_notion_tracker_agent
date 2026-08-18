"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .deps import AppState
from .routes import auth, chat

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.app_state = AppState()
    settings = app.state.app_state.settings
    logger.info("DSS Notion agent starting (model=%s)", settings.gemini_model)

    # Register the OAuth client once at boot so the first /auth/login is fast
    # and a misconfiguration surfaces here rather than mid-flow.
    try:
        client_id = await app.state.app_state.oauth.ensure_registered()
        logger.info("Notion MCP OAuth client ready: %s", client_id)
    except Exception:  # noqa: BLE001 - non-fatal; retried on first login
        logger.warning(
            "OAuth pre-registration failed; will retry on first login",
            exc_info=True,
        )

    try:
        yield
    finally:
        await app.state.app_state.mcp.shutdown()
        logger.info("Shutdown complete")


app = FastAPI(title="DSS Notion Tracker Agent", version="0.1.0", lifespan=lifespan)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_settings.frontend_url],
    allow_credentials=True,  # required: the session cookie rides on every call
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

app.include_router(auth.router)
app.include_router(chat.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
