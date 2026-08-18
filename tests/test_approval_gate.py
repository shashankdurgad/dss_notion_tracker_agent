"""The write-approval gate is the safety-critical path — it gets real tests.

These use a fake MCP manager and a stubbed Gemini client so they run offline.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.genai import types

from backend.agent import NotionAgent, _is_write


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


class FakeModels:
    """Replays a scripted sequence of model turns."""

    def __init__(self, turns: list[types.Content]) -> None:
        self._turns = turns
        self._i = 0

    async def generate_content(self, **kwargs):
        content = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        return type(
            "Resp", (), {"candidates": [type("C", (), {"content": content})()]}
        )()


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


def _agent(monkeypatch, turns: list[types.Content]) -> tuple[NotionAgent, FakeMCP]:
    from backend.config import Settings

    settings = Settings(
        gemini_api_key="test",
        session_secret="s" * 32,
        token_enc_key="k" * 32,
    )
    mcp = FakeMCP()
    monkeypatch.setattr(
        "backend.agent.genai.Client", lambda **_: type("C", (), {"aio": None})()
    )
    agent = NotionAgent(settings, mcp, MemoryStorage())  # type: ignore[arg-type]
    agent._client = type("C", (), {"aio": type("A", (), {"models": FakeModels(turns)})()})()  # type: ignore[attr-defined]
    return agent, mcp


def _call(name: str, args: dict) -> types.Content:
    return types.Content(
        role="model",
        parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
    )


def _text(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def test_write_tools_classified():
    assert _is_write("notion-update-page")
    assert _is_write("notion-create-pages")
    # Unknown mutating tool must fail safe.
    assert _is_write("notion-delete-block")
    assert not _is_write("notion-search")
    assert not _is_write("notion-fetch")
    assert not _is_write("notion-get-comments")


@pytest.mark.asyncio
async def test_write_pauses_and_does_not_execute(monkeypatch):
    agent, mcp = _agent(
        monkeypatch, [_call("notion-update-page", {"page_id": "abc"})]
    )
    events = [e async for e in agent.send("u1", "edit the page")]

    assert any(e["type"] == "approval_required" for e in events)
    # The critical assertion: nothing was written.
    assert mcp.calls == []


@pytest.mark.asyncio
async def test_reject_never_executes(monkeypatch):
    agent, mcp = _agent(
        monkeypatch,
        [_call("notion-update-page", {"page_id": "abc"}), _text("Okay, cancelled.")],
    )
    events = [e async for e in agent.send("u1", "edit")]
    call_id = next(e["call_id"] for e in events if e["type"] == "approval_required")

    after = [e async for e in agent.resume("u1", call_id, "reject")]

    assert mcp.calls == []
    assert any(e["type"] == "tool_rejected" for e in after)


@pytest.mark.asyncio
async def test_approve_executes_once(monkeypatch):
    agent, mcp = _agent(
        monkeypatch,
        [_call("notion-update-page", {"page_id": "abc"}), _text("Done.")],
    )
    events = [e async for e in agent.send("u1", "edit")]
    call_id = next(e["call_id"] for e in events if e["type"] == "approval_required")

    [e async for e in agent.resume("u1", call_id, "approve")]

    assert mcp.calls == [("notion-update-page", {"page_id": "abc"})]


@pytest.mark.asyncio
async def test_reads_execute_without_approval(monkeypatch):
    agent, mcp = _agent(
        monkeypatch, [_call("notion-search", {"query": "minutes"}), _text("Found it.")]
    )
    events = [e async for e in agent.send("u1", "find minutes")]

    assert mcp.calls == [("notion-search", {"query": "minutes"})]
    assert not any(e["type"] == "approval_required" for e in events)


@pytest.mark.asyncio
async def test_stale_call_id_rejected(monkeypatch):
    agent, _ = _agent(monkeypatch, [_text("hi")])
    events = [e async for e in agent.resume("u1", "does-not-exist", "approve")]
    assert events[0]["type"] == "error"
