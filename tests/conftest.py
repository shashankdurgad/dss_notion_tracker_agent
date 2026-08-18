"""Shared test doubles."""

from __future__ import annotations

from typing import Any

from backend.agent import NotionAgent
from backend.llm import LLMResponse, ToolCall


class FakeMCP:
    """Records every tool actually executed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self, user_id: str):
        class T:
            def __init__(self, name: str) -> None:
                self.name = name
                self.description = ""
                self.input_schema = {"type": "object"}

        return [T("notion-search"), T("notion-fetch"), T("notion-update-page")]

    async def call_tool(self, user_id: str, name: str, arguments: dict[str, Any]):
        self.calls.append((name, arguments))

        class R:
            is_error = False
            structured_content = None
            content = [type("B", (), {"text": f"ok:{name}"})()]

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
    return LLMResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])
