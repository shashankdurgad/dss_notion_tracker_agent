"""Shared application state and request-scoped dependencies."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer

from .agent import NotionAgent
from .config import Settings, get_settings
from .mcp_client import NotionMCPManager
from .oauth import NotionOAuthClient
from .tokens import TokenStore

SESSION_COOKIE = "dss_session"
PKCE_TTL_SECONDS = 600  # authorization request must be completed within 10 min


@dataclass
class PendingAuth:
    code_verifier: str
    created_at: float


class AppState:
    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.oauth = NotionOAuthClient(self.settings)
        self.tokens = TokenStore(self.settings, self.oauth)
        self.mcp = NotionMCPManager(self.settings, self.tokens)
        self.agent = NotionAgent(self.settings, self.mcp)
        self.serializer = URLSafeSerializer(
            self.settings.session_secret, salt="dss-session"
        )
        # state -> PKCE verifier, held server-side only.
        self._pending_auth: dict[str, PendingAuth] = {}

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

    def stash_pkce(self, state: str, verifier: str) -> None:
        self._expire_pkce()
        self._pending_auth[state] = PendingAuth(
            code_verifier=verifier, created_at=time.time()
        )

    def take_pkce(self, state: str) -> str | None:
        self._expire_pkce()
        # Constant-time lookup isn't meaningful on a dict, but the state value
        # itself is a 32-byte random token and is single-use.
        entry = self._pending_auth.pop(state, None)
        return entry.code_verifier if entry else None

    def _expire_pkce(self) -> None:
        cutoff = time.time() - PKCE_TTL_SECONDS
        for key in [k for k, v in self._pending_auth.items() if v.created_at < cutoff]:
            self._pending_auth.pop(key, None)


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
