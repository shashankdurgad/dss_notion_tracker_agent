"""Shared application state and request-scoped dependencies."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer

from .agent import NotionAgent
from .config import PROVIDER_NOTION, PROVIDER_SHEETS, Settings, get_settings
from .conversations import ConversationIndex
from .llm import build_provider
from .mcp_client import NotionMCPManager
from .oauth import GoogleOAuthClient, NotionOAuthClient
from .storage import build_storage
from .tokens import TokenStore

SESSION_COOKIE = "dss_session"
PKCE_TTL_SECONDS = 600  # authorization request must be completed within 10 min


class AppState:
    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.storage = build_storage(self.settings)
        self.oauth_clients = {
            PROVIDER_NOTION: NotionOAuthClient(self.settings, self.storage),
            PROVIDER_SHEETS: GoogleOAuthClient(self.settings, self.storage),
        }
        # Notion remains the identity provider; kept as `oauth` so existing
        # call sites (login, callback) read naturally.
        self.oauth = self.oauth_clients[PROVIDER_NOTION]
        self.tokens = TokenStore(self.settings, self.oauth_clients, self.storage)
        self.mcp = NotionMCPManager(self.settings, self.tokens)
        self.provider = build_provider(self.settings)
        self.agent = NotionAgent(
            self.settings, self.mcp, self.storage, self.provider
        )
        self.conversations = ConversationIndex(
            self.storage,
            self.settings.chat_ttl_seconds,
            self.settings.max_conversations,
        )
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

    async def stash_pkce(
        self, state: str, verifier: str, provider: str = PROVIDER_NOTION
    ) -> None:
        await self.storage.set(
            f"pkce:{state}",
            {"verifier": verifier, "provider": provider},
            ttl_seconds=PKCE_TTL_SECONDS,
        )

    async def take_pkce(self, state: str) -> tuple[str, str] | None:
        """Consume a PKCE verifier, returning (verifier, provider).

        Single-use: a replayed state returns None, which is the CSRF defence.
        """
        key = f"pkce:{state}"
        entry = await self.storage.get(key)
        if entry is None:
            return None
        await self.storage.delete(key)
        if isinstance(entry, dict):
            return entry.get("verifier", ""), entry.get("provider", PROVIDER_NOTION)
        # Records written before the provider was stored alongside it.
        if isinstance(entry, str):
            return entry, PROVIDER_NOTION
        return None


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
