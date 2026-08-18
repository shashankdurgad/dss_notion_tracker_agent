"""Notion MCP connection, one per signed-in user.

The MCP Python SDK's transport is an async context manager, so the session
has to live inside a task that holds those contexts open. We run that task
per user and talk to it over a request/response queue.

Verified against mcp==2.0.0:
  streamable_http_client(url, *, http_client=None, terminate_on_close=True)
  Tool.input_schema (snake_case)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import Tool

from .config import NOTION_MCP_URL, USER_AGENT, Settings
from .oauth import TerminalAuthError
from .tokens import TokenStore

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0


class MCPUnauthorized(Exception):
    """Notion rejected the token; caller should refresh and retry once."""


@dataclass
class _Request:
    kind: str  # "list_tools" | "call_tool"
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    future: asyncio.Future = field(default_factory=asyncio.Future)


class NotionMCPSession:
    """Owns one live MCP session, driven by a background task."""

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token
        self._queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        self._tools: list[Tool] = []

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        # Surfaces connection/auth failures here rather than on first tool call.
        await asyncio.wait_for(self._ready, timeout=CONNECT_TIMEOUT)

    async def _run(self) -> None:
        http_client = None
        try:
            http_client = create_mcp_http_client(
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "User-Agent": USER_AGENT,
                }
            )
            async with http_client:
                async with streamable_http_client(
                    NOTION_MCP_URL, http_client=http_client
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        self._tools = list(result.tools)
                        if not self._ready.done():
                            self._ready.set_result(None)
                        await self._serve(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the waiter
            if not self._ready.done():
                self._ready.set_exception(self._translate(exc))
            else:
                logger.exception("Notion MCP session failed")
                self._drain(exc)

    async def _serve(self, session: ClientSession) -> None:
        while True:
            request = await self._queue.get()
            if request is None:
                return
            try:
                if request.kind == "list_tools":
                    result = await session.list_tools()
                    value: Any = list(result.tools)
                else:
                    value = await asyncio.wait_for(
                        session.call_tool(request.name, request.arguments),
                        timeout=CALL_TIMEOUT,
                    )
                if not request.future.done():
                    request.future.set_result(value)
            except Exception as exc:  # noqa: BLE001 - returned to the caller
                if not request.future.done():
                    request.future.set_exception(self._translate(exc))

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        text = str(exc).lower()
        if "401" in text or "unauthorized" in text or "invalid_token" in text:
            return MCPUnauthorized("Notion rejected the access token.")
        return exc

    def _drain(self, exc: Exception) -> None:
        while not self._queue.empty():
            pending = self._queue.get_nowait()
            if pending and not pending.future.done():
                pending.future.set_exception(exc)

    @property
    def tools(self) -> list[Tool]:
        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._task is None or self._task.done():
            raise MCPUnauthorized("Notion session is not connected.")
        request = _Request(kind="call_tool", name=name, arguments=arguments)
        await self._queue.put(request)
        return await request.future

    async def close(self) -> None:
        await self._queue.put(None)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


class NotionMCPManager:
    """Caches one session per user and handles 401 -> refresh -> reconnect."""

    def __init__(self, settings: Settings, tokens: TokenStore) -> None:
        self._settings = settings
        self._tokens = tokens
        self._sessions: dict[str, NotionMCPSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def session(self, user_id: str, *, force_new: bool = False) -> NotionMCPSession:
        async with self._lock(user_id):
            existing = self._sessions.get(user_id)
            if existing is not None and not force_new:
                return existing
            if existing is not None:
                await existing.close()
                self._sessions.pop(user_id, None)

            access_token = await self._tokens.get_access_token(user_id)
            session = NotionMCPSession(access_token)
            await session.start()
            self._sessions[user_id] = session
            return session

    async def list_tools(self, user_id: str) -> list[Tool]:
        return (await self.session(user_id)).tools

    async def call_tool(
        self, user_id: str, name: str, arguments: dict[str, Any]
    ) -> Any:
        session = await self.session(user_id)
        try:
            return await session.call_tool(name, arguments)
        except MCPUnauthorized:
            # Token died mid-session. Refresh happens inside session(); if the
            # grant itself is dead this raises TerminalAuthError and the caller
            # prompts for re-login. Retry exactly once — never loop.
            session = await self.session(user_id, force_new=True)
            return await session.call_tool(name, arguments)

    async def disconnect(self, user_id: str) -> None:
        async with self._lock(user_id):
            session = self._sessions.pop(user_id, None)
        if session is not None:
            await session.close()

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()


async def probe_workspace(manager: NotionMCPManager, user_id: str) -> dict[str, Any]:
    """Call notion-get-self to identify the workspace and check tool availability.

    Notion MCP is Beta and `notion-search` requires Notion AI on the workspace,
    so we report what's actually available rather than assuming.
    """
    tools = await manager.list_tools(user_id)
    names = {tool.name for tool in tools}

    workspace_name: str | None = None
    if "notion-get-self" in names:
        try:
            result = await manager.call_tool(user_id, "notion-get-self", {})
            workspace_name = _extract_workspace_name(result)
        except TerminalAuthError:
            raise
        except Exception:  # noqa: BLE001 - identity is best-effort
            logger.warning("notion-get-self failed", exc_info=True)

    return {
        "workspace_name": workspace_name,
        "tools": sorted(names),
        "search_available": "notion-search" in names,
    }


def _extract_workspace_name(result: Any) -> str | None:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        for key in ("workspace_name", "workspaceName", "name"):
            value = structured.get(key)
            if isinstance(value, str) and value:
                return value

    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            import json

            try:
                data = json.loads(text)
            except ValueError:
                return text.strip()[:120]
            if isinstance(data, dict):
                for key in ("workspace_name", "workspaceName", "name"):
                    value = data.get(key)
                    if isinstance(value, str) and value:
                        return value
    return None
