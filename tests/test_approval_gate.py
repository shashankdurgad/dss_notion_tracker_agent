"""The write-approval gate is the safety-critical path, so it gets real tests."""

from __future__ import annotations

import pytest

from backend.agent import _is_write
from tests.conftest import ScriptedProvider, call_response, make_agent, text_response


def test_write_tools_classified():
    assert _is_write("notion-update-page")
    assert _is_write("notion-create-pages")
    # Unknown mutating tool must fail safe.
    assert _is_write("notion-delete-block")
    assert not _is_write("notion-search")
    assert not _is_write("notion-fetch")
    assert not _is_write("notion-get-comments")


@pytest.mark.asyncio
async def test_write_pauses_and_does_not_execute():
    agent, mcp = make_agent(
        ScriptedProvider([call_response("notion-update-page", {"page_id": "abc"})])
    )
    events = [e async for e in agent.send("u1", "edit the page")]

    assert any(e["type"] == "approval_required" for e in events)
    # The critical assertion: nothing was written.
    assert mcp.calls == []


@pytest.mark.asyncio
async def test_reject_never_executes():
    agent, mcp = make_agent(
        ScriptedProvider(
            [
                call_response("notion-update-page", {"page_id": "abc"}),
                text_response("Okay, cancelled."),
            ]
        )
    )
    events = [e async for e in agent.send("u1", "edit")]
    call_id = next(e["call_id"] for e in events if e["type"] == "approval_required")

    after = [e async for e in agent.resume("u1", call_id, "reject")]

    assert mcp.calls == []
    assert any(e["type"] == "tool_rejected" for e in after)


@pytest.mark.asyncio
async def test_approve_executes_once():
    agent, mcp = make_agent(
        ScriptedProvider(
            [
                call_response("notion-update-page", {"page_id": "abc"}),
                text_response("Done."),
            ]
        )
    )
    events = [e async for e in agent.send("u1", "edit")]
    call_id = next(e["call_id"] for e in events if e["type"] == "approval_required")

    [e async for e in agent.resume("u1", call_id, "approve")]

    assert mcp.calls == [("notion-update-page", {"page_id": "abc"})]


@pytest.mark.asyncio
async def test_reads_execute_without_approval():
    agent, mcp = make_agent(
        ScriptedProvider(
            [
                call_response("notion-search", {"query": "minutes"}),
                text_response("Found it."),
            ]
        )
    )
    events = [e async for e in agent.send("u1", "find minutes")]

    assert mcp.calls == [("notion-search", {"query": "minutes"})]
    assert not any(e["type"] == "approval_required" for e in events)


@pytest.mark.asyncio
async def test_stale_call_id_rejected():
    agent, _ = make_agent(ScriptedProvider([text_response("hi")]))
    events = [e async for e in agent.resume("u1", "does-not-exist", "approve")]
    assert events[0]["type"] == "error"
