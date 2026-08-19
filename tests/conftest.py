"""Shared test doubles."""

from __future__ import annotations

from typing import Any

from backend.agent import NotionAgent
from backend.config import PREFIX_SEPARATOR
from backend.llm import LLMResponse, ToolCall


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = ""
        self.input_schema = {"type": "object"}


class FakeMCP:
    """Records every tool actually executed, and which server it went to."""

    #: provider -> tool names it serves
    TOOLS = {
        "notion": ["notion-search", "notion-fetch", "notion-update-page"],
        "sheets": ["get_values", "get_spreadsheet", "update_values", "insert_dimension"],
    }

    def __init__(self, providers: list[str] | None = None) -> None:
        # (tool_name, arguments) — what the old tests assert on.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # (provider, tool_name, arguments) — for routing assertions.
        self.routed: list[tuple[str, str, dict[str, Any]]] = []
        self._providers = providers if providers is not None else ["notion"]

    async def connected_providers(self, user_id: str) -> list[str]:
        return list(self._providers)

    async def list_tools(self, user_id: str, provider: str = "notion"):
        return [FakeTool(n) for n in self.TOOLS.get(provider, [])]

    async def call_tool(
        self,
        user_id: str,
        name: str,
        arguments: dict[str, Any],
        provider: str = "notion",
    ):
        self.calls.append((name, arguments))
        self.routed.append((provider, name, arguments))

        class R:
            is_error = False
            structured_content = None
            content = [type("B", (), {"text": f"ok:{provider}:{name}"})()]

        return R()


class MemoryStorage:
    """In-process Storage implementation for tests."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value, ttl_seconds: int | None = None) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


class ScriptedProvider:
    """Replays a fixed sequence of model responses."""

    name = "scripted"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0
        self.last_messages: list = []

    async def generate(self, *, system, messages, tools):
        self.last_messages = list(messages)
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


class FailingProvider:
    """Raises `failures` times, then returns `then`."""

    name = "failing"

    def __init__(self, failures: int, exc: Exception, then: LLMResponse) -> None:
        self.failures = failures
        self.exc = exc
        self.then = then
        self.attempts = 0

    async def generate(self, *, system, messages, tools):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.exc
        return self.then


def make_settings():
    from backend.config import Settings

    return Settings(
        llm_provider="gemini",
        gemini_api_key="test",
        session_secret="s" * 32,
        token_enc_key="k" * 32,
    )


def make_agent(provider, storage=None, mcp=None) -> tuple[NotionAgent, FakeMCP]:
    mcp = mcp or FakeMCP()
    agent = NotionAgent(
        make_settings(), mcp, storage or MemoryStorage(), provider
    )  # type: ignore[arg-type]
    return agent, mcp


def text_response(text: str) -> LLMResponse:
    return LLMResponse(text=text)


def call_response(name: str, args: dict, call_id: str = "c1") -> LLMResponse:
    """A model turn requesting one tool.

    Bare names are prefixed with `notion__` to match what the agent now
    exposes to the model; pass an already-prefixed name to target Sheets.
    """
    if PREFIX_SEPARATOR not in name:
        name = f"notion{PREFIX_SEPARATOR}{name}"
    return LLMResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])
