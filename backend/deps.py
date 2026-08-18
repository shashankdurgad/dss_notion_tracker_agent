"""Shared application state and request-scoped dependencies."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer

from .agent import NotionAgent
from .config import Settings, get_settings
from .mcp_client import NotionMCPManager
from .oauth import NotionOAuthClient
from .storage import build_storage
from .tokens import TokenStore

SESSION_COOKIE = "dss_session"
PKCE_TTL_SECONDS = 600  # authorization request must be completed within 10 min


class AppState:
    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.storage = build_storage(self.settings)
        self.oauth = NotionOAuthClient(self.settings, self.storage)
        self.tokens = TokenStore(self.settings, self.oauth, self.storage)
        self.mcp = NotionMCPManager(self.settings, self.tokens)
        self.agent = NotionAgent(self.settings, self.mcp, self.storage)
        self.serializer = URLSafeSerializer(
            self.settings.session_secret, salt="dss-session"
        )

    # ---------- session cookie ----------

    def new_user_id(self) -> str:
        return secrets.token_urlsafe(24)

    def sign(self, user_id: str) -> str:
        return self.serializer.dumps({"uid": user_id})

    def unsign(self, raw: str) -> str | None:
        try:
            data: Any = self.serializer.loads(raw)
        except BadSignature:
            return None
        uid = data.get("uid") if isinstance(data, dict) else None
        return uid if isinstance(uid, str) else None

    # ---------- PKCE state ----------
    #
    # Held in shared storage rather than memory: on serverless the /auth/login
    # and /auth/callback requests routinely land on different instances, so an
    # in-process dict would fail every sign-in. The TTL expires abandoned
    # authorization attempts without a sweeper.

    async def stash_pkce(self, state: str, verifier: str) -> None:
        await self.storage.set(
            f"pkce:{state}", verifier, ttl_seconds=PKCE_TTL_SECONDS
        )

    async def take_pkce(self, state: str) -> str | None:
        """Consume a PKCE verifier. Single-use: a replayed state returns None."""
        key = f"pkce:{state}"
        verifier = await self.storage.get(key)
        if verifier is None:
            return None
        await self.storage.delete(key)
        return verifier if isinstance(verifier, str) else None


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def current_user(request: Request) -> str:
    """User id from the signed cookie, or 401."""
    state = get_state(request)
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="Not signed in.")
    user_id = state.unsign(raw)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid session.")
    return user_id


def optional_user(request: Request) -> str | None:
    try:
        return current_user(request)
    except HTTPException:
        return None
