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

USER_AGENT = "DSS-Notion-Agent/1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    gemini_api_key: str
    gemini_model: str = "gemini-3.7-flash"

    session_secret: str
    token_enc_key: str

    oauth_redirect_uri: str = "http://localhost:8000/auth/callback"
    frontend_url: str = "http://localhost:5173"

    notion_root_page_id: str = ""
    state_dir: Path = Path(".state")

    # Refresh the access token this many seconds before it actually expires,
    # so an in-flight request never races the expiry.
    refresh_skew_seconds: int = 300

    # Hard ceiling on agent tool-calling iterations per turn.
    max_agent_iterations: int = 10

    @property
    def client_registration_path(self) -> Path:
        return self.state_dir / "oauth_client.json"

    @property
    def token_store_path(self) -> Path:
        return self.state_dir / "tokens.json"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings
