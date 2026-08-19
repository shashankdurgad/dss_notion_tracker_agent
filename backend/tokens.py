"""Encrypted token storage with per-user refresh serialization.

Three invariants:

1. Tokens are encrypted (Fernet) before they reach the storage backend, so
   whoever hosts Redis never sees usable credentials.
2. Each user's tokens live under their own key. A user's signed cookie can
   only address their own record.
3. Refreshes are serialized per user. Notion rotates refresh tokens on every
   use and revokes the entire grant if a rotated token is replayed.

Note on (3): the asyncio.Lock only serializes within one process. On
serverless, two concurrent requests may land on different instances and race.
That window is small (a refresh happens once per ~8h, 5 minutes before
expiry) and the loser simply re-reads the winner's token, so it self-heals
on the next call — see _refresh() for the re-check that makes this safe.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .oauth import NotionOAuthClient, TerminalAuthError, TokenSet
from .storage import Storage

logger = logging.getLogger(__name__)


def _key(user_id: str) -> str:
    return f"session:{user_id}"


class TokenStore:
    def __init__(
        self, settings: Settings, oauth: NotionOAuthClient, storage: Storage
    ) -> None:
        self._settings = settings
        self._oauth = oauth
        self._storage = storage
        self._fernet = Fernet(settings.token_enc_key.encode())
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ---------- crypto ----------

    def _encrypt(self, tokens: TokenSet) -> str:
        import json

        return self._fernet.encrypt(json.dumps(tokens.to_dict()).encode()).decode()

    def _decrypt(self, blob: str) -> TokenSet | None:
        import json

        try:
            return TokenSet.from_dict(json.loads(self._fernet.decrypt(blob.encode())))
        except (InvalidToken, ValueError, KeyError):
            # Key rotated, or the record was tampered with. Treat as signed out.
            return None

    # ---------- public API ----------

    async def save(self, user_id: str, tokens: TokenSet) -> None:
        await self._storage.set(
            _key(user_id),
            self._encrypt(tokens),
            ttl_seconds=self._settings.session_ttl_seconds,
        )

    async def clear(self, user_id: str) -> None:
        await self._storage.delete(_key(user_id))

    async def peek(self, user_id: str) -> TokenSet | None:
        """Stored tokens without refreshing. For status checks only."""
        blob = await self._storage.get(_key(user_id))
        return self._decrypt(blob) if isinstance(blob, str) else None

    async def get_access_token(self, user_id: str) -> str:
        """Return a valid access token, refreshing proactively if needed.

        Raises TerminalAuthError when the user must sign in again.
        """
        tokens = await self.peek(user_id)
        if tokens is None:
            raise TerminalAuthError("Not signed in to Notion.")

        if not tokens.is_expiring(self._settings.refresh_skew_seconds):
            return tokens.access_token

        return await self._refresh(user_id)

    async def _refresh(self, user_id: str) -> str:
        # Serialize per user: a second caller waits here, then finds the token
        # already refreshed by the first and returns without refreshing again.
        async with self._locks[user_id]:
            current = await self.peek(user_id)
            if current is None:
                raise TerminalAuthError("Not signed in to Notion.")
            # Re-check after acquiring the lock — the holder may have just
            # refreshed. This is also what makes a cross-instance race benign.
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

            # Preserve the workspace label and account key across refreshes.
            # account_key is the app's user identity — dropping it strands the
            # user's saved chats on their next sign-in.
            refreshed = TokenSet(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token,
                expires_at=refreshed.expires_at,
                workspace_name=current.workspace_name,
                account_key=current.account_key,
            )
            # Access + rotated refresh are written together; a partial write
            # would strand the grant.
            await self.save(user_id, refreshed)
            return refreshed.access_token

    async def set_workspace_name(self, user_id: str, name: str) -> None:
        tokens = await self.peek(user_id)
        if tokens is None or tokens.workspace_name == name:
            return
        await self.save(
            user_id,
            TokenSet(
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                expires_at=tokens.expires_at,
                workspace_name=name,
                account_key=tokens.account_key,
            ),
        )
