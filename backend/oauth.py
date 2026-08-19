"""OAuth 2.1 client for Notion's hosted MCP server.

Flow: discovery (RFC 9728 -> RFC 8414) -> dynamic client registration
(RFC 7591) -> authorization code + PKCE S256 -> token exchange -> refresh.

Notion MCP is Beta, so every endpoint is *discovered* rather than hardcoded.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import NOTION_MCP_BASE, USER_AGENT, Settings

DISCOVERY_TIMEOUT = 15.0

# Storage key for the dynamically registered OAuth client. Shared across all
# instances so a cold start reuses the existing registration.
CLIENT_REGISTRATION_KEY = "oauth:client"


class OAuthError(Exception):
    """Recoverable OAuth failure — surfaced to the user as 'please sign in'."""


class TerminalAuthError(OAuthError):
    """The grant is dead (invalid_grant). Must re-authorize; never retry."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _account_key(payload: dict[str, Any]) -> str | None:
    """Derive a stable per-account id from Notion's token response.

    Lets a returning user reattach their saved chats instead of getting a
    fresh random identity on every sign-in. Hashed so raw Notion identifiers
    never become storage keys, and scoped to workspace + user so two people
    sharing a workspace never share chats.
    """
    raw_user = payload.get("user_id") or (payload.get("owner") or {}).get("user", {}).get("id")
    workspace = payload.get("workspace_id") or ""
    if not raw_user:
        return None
    digest = hashlib.sha256(f"{workspace}:{raw_user}".encode()).hexdigest()
    return digest[:32]


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


@dataclass(frozen=True)
class ServerMetadata:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None
    scopes_supported: list[str]

    @property
    def scope(self) -> str:
        return " ".join(self.scopes_supported) if self.scopes_supported else "default"


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float
    workspace_name: str | None = None
    # Stable identity from Notion, used to reattach saved chats when the same
    # person signs in again. None if Notion didn't return identity fields.
    account_key: str | None = None

    def is_expiring(self, skew_seconds: int) -> bool:
        return time.time() >= (self.expires_at - skew_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "workspace_name": self.workspace_name,
            "account_key": self.account_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenSet:
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=data["expires_at"],
            workspace_name=data.get("workspace_name"),
            account_key=data.get("account_key"),
        )

    @classmethod
    def from_token_response(
        cls, payload: dict[str, Any], *, fallback_refresh: str | None = None
    ) -> TokenSet:
        # Trust the server's expires_in; never hardcode a lifetime.
        expires_in = float(payload.get("expires_in", 3600))
        return cls(
            access_token=payload["access_token"],
            # Refresh tokens rotate on every use. If the server omits a new one,
            # the old one stays valid — keep it rather than dropping the grant.
            refresh_token=payload.get("refresh_token") or fallback_refresh,
            expires_at=time.time() + expires_in,
            account_key=_account_key(payload),
        )


class NotionOAuthClient:
    """Discovery + registration + token operations. Stateless per call."""

    def __init__(self, settings: Settings, storage: Any = None) -> None:
        self._settings = settings
        self._storage = storage
        self._metadata: ServerMetadata | None = None
        self._client_id: str | None = None
        self._client_secret: str | None = None

    # ---------- discovery ----------

    async def metadata(self) -> ServerMetadata:
        if self._metadata is not None:
            return self._metadata

        async with httpx.AsyncClient(
            timeout=DISCOVERY_TIMEOUT, headers={"User-Agent": USER_AGENT}
        ) as http:
            # RFC 9728: protected resource metadata points at its auth server(s).
            auth_base = NOTION_MCP_BASE
            try:
                resource = await http.get(
                    f"{NOTION_MCP_BASE}/.well-known/oauth-protected-resource"
                )
                if resource.status_code == 200:
                    servers = resource.json().get("authorization_servers") or []
                    if servers:
                        auth_base = servers[0].rstrip("/")
            except httpx.HTTPError:
                # Fall back to the MCP base; the next call is the real check.
                pass

            response = await http.get(
                f"{auth_base}/.well-known/oauth-authorization-server"
            )
            if response.status_code != 200:
                raise OAuthError(
                    f"OAuth discovery failed at {auth_base} "
                    f"(HTTP {response.status_code})"
                )
            doc = response.json()

        for required in ("authorization_endpoint", "token_endpoint"):
            if not doc.get(required):
                raise OAuthError(f"Discovery document missing {required}")

        self._metadata = ServerMetadata(
            authorization_endpoint=doc["authorization_endpoint"],
            token_endpoint=doc["token_endpoint"],
            registration_endpoint=doc.get("registration_endpoint"),
            revocation_endpoint=doc.get("revocation_endpoint"),
            scopes_supported=doc.get("scopes_supported") or [],
        )
        return self._metadata

    # ---------- dynamic client registration ----------

    async def ensure_registered(self) -> str:
        """Return our client_id, registering once and caching to disk.

        Re-registering on every boot would leak a new client record each time,
        so the result is persisted under STATE_DIR.
        """
        if self._client_id:
            return self._client_id

        # Shared across instances: on serverless, re-registering per cold start
        # would leak a new client record on every scale-up.
        saved = await self._storage.get(CLIENT_REGISTRATION_KEY) if self._storage else None
        if isinstance(saved, dict):
            # Registration is bound to the redirect URI; if that changed, redo it.
            if saved.get("redirect_uri") == self._settings.oauth_redirect_uri:
                self._client_id = saved["client_id"]
                self._client_secret = saved.get("client_secret")
                return self._client_id

        meta = await self.metadata()
        if not meta.registration_endpoint:
            raise OAuthError(
                "Notion MCP does not advertise a registration endpoint and no "
                "client_id is configured."
            )

        payload = {
            "client_name": "DSS Notion Tracker Agent",
            "redirect_uris": [self._settings.oauth_redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        async with httpx.AsyncClient(
            timeout=DISCOVERY_TIMEOUT, headers={"User-Agent": USER_AGENT}
        ) as http:
            response = await http.post(meta.registration_endpoint, json=payload)
        if response.status_code not in (200, 201):
            raise OAuthError(
                f"Dynamic client registration failed "
                f"(HTTP {response.status_code}): {response.text[:300]}"
            )

        data = response.json()
        self._client_id = data["client_id"]
        self._client_secret = data.get("client_secret")

        if self._storage:
            await self._storage.set(
                CLIENT_REGISTRATION_KEY,
                {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": self._settings.oauth_redirect_uri,
                },
            )
        return self._client_id

    # ---------- authorization ----------

    async def authorization_url(self, *, state: str, code_challenge: str) -> str:
        meta = await self.metadata()
        client_id = await self.ensure_registered()
        params = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": self._settings.oauth_redirect_uri,
                "scope": meta.scope,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{meta.authorization_endpoint}?{params}"

    async def exchange_code(self, *, code: str, code_verifier: str) -> TokenSet:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._settings.oauth_redirect_uri,
            "client_id": await self.ensure_registered(),
            "code_verifier": code_verifier,
        }
        return TokenSet.from_token_response(await self._post_token(data))

    async def refresh(self, refresh_token: str) -> TokenSet:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": await self.ensure_registered(),
        }
        payload = await self._post_token(data)
        return TokenSet.from_token_response(payload, fallback_refresh=refresh_token)

    async def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        meta = await self.metadata()
        if self._client_secret:
            data = {**data, "client_secret": self._client_secret}

        async with httpx.AsyncClient(
            timeout=DISCOVERY_TIMEOUT, headers={"User-Agent": USER_AGENT}
        ) as http:
            response = await http.post(meta.token_endpoint, data=data)

        if response.status_code == 200:
            return response.json()

        error_code = ""
        try:
            error_code = response.json().get("error", "")
        except ValueError:
            pass

        # invalid_grant means the grant is gone (rotated token replayed, expired,
        # or revoked). Retrying makes it worse — Notion revokes the whole grant.
        if error_code == "invalid_grant":
            raise TerminalAuthError("Notion authorization expired; sign in again.")
        raise OAuthError(
            f"Token request failed (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )
