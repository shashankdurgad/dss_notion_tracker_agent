"""Environment-backed settings.

Everything secret comes from the environment; nothing here has a usable
default that would let the app start misconfigured but appear to work.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Notion's hosted MCP server. The OAuth endpoints are *discovered* at runtime
# from this base (see oauth.py) rather than hardcoded — Notion MCP is Beta and
# the discovery documents are the source of truth.
NOTION_MCP_URL = "https://mcp.notion.com/mcp"
NOTION_MCP_BASE = "https://mcp.notion.com"

# Google's official Sheets MCP server. Its protected-resource metadata is
# path-scoped (/.well-known/oauth-protected-resource/mcp/v1), not served from
# the domain root like Notion's — see GoogleOAuthClient.
GOOGLE_MCP_URL = "https://sheetsmcp.googleapis.com/mcp/v1"
GOOGLE_MCP_BASE = "https://sheetsmcp.googleapis.com"

# drive.file limits the agent to spreadsheets the user explicitly picks,
# rather than everything in their Drive.
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/spreadsheets"
)

PROVIDER_NOTION = "notion"
PROVIDER_SHEETS = "sheets"
PROVIDERS = (PROVIDER_NOTION, PROVIDER_SHEETS)

# Tool names are exposed to the model as `<provider>__<tool>` so a call can be
# routed back to the server that owns it. Double underscore because both
# Gemini and OpenAI-compatible APIs restrict tool names to [a-zA-Z0-9_.-].
PREFIX_SEPARATOR = "__"

USER_AGENT = "DSS-Notion-Agent/1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # "gemini" or "openrouter". OpenRouter's free tier is a useful escape
    # hatch when Gemini rate-limits, at the cost of model quality.
    llm_provider: str = "gemini"

    # Optional so the app can run on either provider alone.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"

    openrouter_api_key: str = ""
    openrouter_model: str = "nvidia/nemotron-3-super-120b-a12b:free"

    session_secret: str
    token_enc_key: str

    oauth_redirect_uri: str = "http://localhost:8000/auth/callback"
    frontend_url: str = "http://localhost:5173"

    # Google has no dynamic client registration, so these are created by hand
    # in Google Cloud Console. Without them the Sheets connection is disabled
    # rather than failing at request time.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_scopes: str = GOOGLE_SCOPES

    notion_root_page_id: str = ""
    state_dir: Path = Path(".state")

    # Upstash Redis. Set both to run on serverless (Vercel), where the
    # filesystem is ephemeral and instances don't share memory. Left empty,
    # storage falls back to a local file for development.
    redis_url: str = ""
    redis_token: str = ""

    # How long a signed-in session's tokens survive without use. Notion's
    # refresh tokens die after 30 idle days, so outliving that is pointless.
    session_ttl_seconds: int = 60 * 60 * 24 * 30

    # Saved conversations live as long as the login session, so a chat is
    # still there next time you sign in. Bounded so Redis can't grow forever
    # on the free tier.
    chat_ttl_seconds: int = 60 * 60 * 24 * 30

    # Cap on saved conversations per user; the oldest is dropped beyond this.
    max_conversations: int = 50

    # Refresh the access token this many seconds before it actually expires,
    # so an in-flight request never races the expiry.
    refresh_skew_seconds: int = 300

    # Hard ceiling on agent tool-calling iterations per turn.
    max_agent_iterations: int = 10

    @property
    def uses_redis(self) -> bool:
        return bool(self.redis_url and self.redis_token)

    @property
    def sheets_configured(self) -> bool:
        """Whether Google Sheets can be offered at all."""
        return bool(self.google_client_id and self.google_client_secret)

    def validate_provider(self) -> None:
        """Fail at startup rather than on the first user message."""
        if self.llm_provider not in ("gemini", "openrouter"):
            raise ValueError(
                f"LLM_PROVIDER must be 'gemini' or 'openrouter', "
                f"got {self.llm_provider!r}"
            )
        if self.llm_provider == "gemini" and not self.gemini_api_key:
            raise ValueError("LLM_PROVIDER=gemini requires GEMINI_API_KEY")
        if self.llm_provider == "openrouter" and not self.openrouter_api_key:
            raise ValueError("LLM_PROVIDER=openrouter requires OPENROUTER_API_KEY")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    settings.validate_provider()
    # Serverless filesystems are read-only outside /tmp, so only create the
    # local state directory when we're actually using file storage.
    if not settings.uses_redis:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings
