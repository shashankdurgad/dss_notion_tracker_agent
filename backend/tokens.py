"""Encrypted token storage with per-user refresh serialization.

Two invariants matter here:

1. Tokens are encrypted at rest (Fernet) and never leave the server.
2. Refreshes are serialized per user. Notion rotates refresh tokens on every
   use and revokes the entire grant if a rotated token is replayed — so two
   concurrent refreshes for one user would log them out permanently.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .oauth import NotionOAuthClient, TerminalAuthError, TokenSet


class TokenStore:
    def __init__(self, settings: Settings, oauth: NotionOAuthClient) -> None:
        self._settings = settings
        self._oauth = oauth
        self._fernet = Fernet(settings.token_enc_key.encode())
        self._path: Path = settings.token_store_path
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._cache: dict[str, TokenSet] = {}
        self._io_lock = asyncio.Lock()
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (ValueError, OSError):
            return
        for user_id, blob in raw.items():
            try:
                decrypted = self._fernet.decrypt(blob.encode())
            except InvalidToken:
                # Key rotated or file tampered with — drop it; user re-authorizes.
                continue
            self._cache[user_id] = TokenSet.from_dict(json.loads(decrypted))

    def _flush(self) -> None:
        payload = {
            user_id: self._fernet.encrypt(
                json.dumps(tokens.to_dict()).encode()
            ).decode()
            for user_id, tokens in self._cache.items()
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write can't truncate the store and
        # lose a rotated refresh token.
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2))
        temp.chmod(0o600)
        temp.replace(self._path)

    # ---------- public API ----------

    async def save(self, user_id: str, tokens: TokenSet) -> None:
        async with self._io_lock:
            self._cache[user_id] = tokens
            self._flush()

    async def clear(self, user_id: str) -> None:
        async with self._io_lock:
            self._cache.pop(user_id, None)
            self._flush()

    def peek(self, user_id: str) -> TokenSet | None:
        """Stored tokens without refreshing. For status checks only."""
        return self._cache.get(user_id)

    async def get_access_token(self, user_id: str) -> str:
        """Return a valid access token, refreshing proactively if needed.

        Raises TerminalAuthError when the user must sign in again.
        """
        tokens = self._cache.get(user_id)
        if tokens is None:
            raise TerminalAuthError("Not signed in to Notion.")

        if not tokens.is_expiring(self._settings.refresh_skew_seconds):
            return tokens.access_token

        # Serialize per user: a second caller waits here, then finds the token
        # already refreshed by the first and returns without a second refresh.
        async with self._locks[user_id]:
            current = self._cache.get(user_id)
            if current is None:
                raise TerminalAuthError("Not signed in to Notion.")
            if not current.is_expiring(self._settings.refresh_skew_seconds):
                return current.access_token

            if not current.refresh_token:
                await self.clear(user_id)
                raise TerminalAuthError("Notion session expired; sign in again.")

            try:
                refreshed = await self._oauth.refresh(current.refresh_token)
            except TerminalAuthError:
                await self.clear(user_id)
                raise

            # Preserve the workspace label across refreshes.
            refreshed = TokenSet(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token,
                expires_at=refreshed.expires_at,
                workspace_name=current.workspace_name,
            )
            # Access + rotated refresh land together — a partial write here
            # would strand the grant.
            await self.save(user_id, refreshed)
            return refreshed.access_token

    async def set_workspace_name(self, user_id: str, name: str) -> None:
        tokens = self._cache.get(user_id)
        if tokens is None or tokens.workspace_name == name:
            return
        await self.save(
            user_id,
            TokenSet(
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                expires_at=tokens.expires_at,
                workspace_name=name,
            ),
        )
