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

from .config import PROVIDER_NOTION, Settings
from .oauth import NotionOAuthClient, TerminalAuthError, TokenSet
from .storage import Storage

logger = logging.getLogger(__name__)


def _key(user_id: str, provider: str) -> str:
    return f"session:{user_id}:{provider}"


def _legacy_key(user_id: str) -> str:
    """Pre-multi-provider key, when Notion was the only connection.

    Still read so an existing deploy's users aren't signed out; rewritten to
    the namespaced key on the next save.
    """
    return f"session:{user_id}"


class TokenStore:
    def __init__(
        self,
        settings: Settings,
        clients: dict[str, NotionOAuthClient],
        storage: Storage,
    ) -> None:
        self._settings = settings
        self._clients = clients
        self._storage = storage
        self._fernet = Fernet(settings.token_enc_key.encode())
        # Keyed per (user, provider): one provider's refresh must not block
        # the other's.
        self._locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

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

    async def save(
        self, user_id: str, tokens: TokenSet, provider: str = PROVIDER_NOTION
    ) -> None:
        await self._storage.set(
            _key(user_id, provider),
            self._encrypt(tokens),
            ttl_seconds=self._settings.session_ttl_seconds,
        )

    async def clear(self, user_id: str, provider: str = PROVIDER_NOTION) -> None:
        await self._storage.delete(_key(user_id, provider))
        if provider == PROVIDER_NOTION:
            await self._storage.delete(_legacy_key(user_id))

    async def peek(
        self, user_id: str, provider: str = PROVIDER_NOTION
    ) -> TokenSet | None:
        """Stored tokens without refreshing. For status checks only."""
        blob = await self._storage.get(_key(user_id, provider))
        if not isinstance(blob, str) and provider == PROVIDER_NOTION:
            # Records written before providers were namespaced.
            blob = await self._storage.get(_legacy_key(user_id))
        return self._decrypt(blob) if isinstance(blob, str) else None

    async def connected(self, user_id: str, provider: str) -> bool:
        return await self.peek(user_id, provider) is not None

    async def get_access_token(
        self, user_id: str, provider: str = PROVIDER_NOTION
    ) -> str:
        """Return a valid access token, refreshing proactively if needed.

        Raises TerminalAuthError when the user must connect again.
        """
        tokens = await self.peek(user_id, provider)
        if tokens is None:
            raise TerminalAuthError(f"Not connected to {provider}.")

        if not tokens.is_expiring(self._settings.refresh_skew_seconds):
            return tokens.access_token

        return await self._refresh(user_id, provider)

    async def _refresh(self, user_id: str, provider: str) -> str:
        # Serialize per (user, provider): a second caller waits here, then
        # finds the token already refreshed and returns without refreshing.
        async with self._locks[(user_id, provider)]:
            current = await self.peek(user_id, provider)
            if current is None:
                raise TerminalAuthError(f"Not connected to {provider}.")
            # Re-check after acquiring the lock — the holder may have just
            # refreshed. This is also what makes a cross-instance race benign.
            if not current.is_expiring(self._settings.refresh_skew_seconds):
                return current.access_token

            if not current.refresh_token:
                await self.clear(user_id, provider)
                raise TerminalAuthError(
                    f"{provider} session expired; connect again."
                )

            client = self._clients.get(provider)
            if client is None:
                raise TerminalAuthError(f"No OAuth client for {provider}.")

            try:
                refreshed = await client.refresh(current.refresh_token)
            except TerminalAuthError:
                await self.clear(user_id, provider)
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
            await self.save(user_id, refreshed, provider)
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
