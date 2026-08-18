"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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

    # On a persistent server, register the OAuth client at boot so the first
    # /auth/login is fast and a misconfiguration surfaces here rather than
    # mid-flow. On serverless this would run on every cold start and add
    # latency to unrelated requests, so it's deferred to the login path
    # (which caches the registration in shared storage anyway).
    if not settings.uses_redis:
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


# Serve the built frontend from the same origin as the API.
#
# Same-origin matters: the session cookie is SameSite=Lax, so a cross-origin
# frontend would have it dropped on the OAuth redirect back from Notion.
# Mounted last so it can't shadow the API routes above. On Vercel these files
# are promoted to the CDN at build time rather than served by the function.
_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=_FRONTEND_DIST, html=True),
        name="frontend",
    )
else:
    logger.info("No frontend build at %s; serving API only", _FRONTEND_DIST)
