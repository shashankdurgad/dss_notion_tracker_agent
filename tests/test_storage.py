"""State must survive across instances, and stay isolated between users."""

from __future__ import annotations

import pytest

from backend.storage import FileStorage
from tests.conftest import (
    MemoryStorage,
    ScriptedProvider,
    call_response,
    make_agent,
    text_response,
)


@pytest.mark.asyncio
async def test_history_survives_instance_swap():
    """A second 'instance' sharing storage sees the first one's conversation."""
    storage = MemoryStorage()

    first, _ = make_agent(ScriptedProvider([text_response("Hello there.")]), storage)
    [e async for e in first.send("alice", "hi")]

    # Simulate a cold start: brand new agent object, same shared storage.
    second, _ = make_agent(ScriptedProvider([text_response("Still here.")]), storage)
    session = await second.load_session("alice")

    assert len(session.history) == 2  # user turn + assistant reply
    assert session.history[0].text == "hi"


@pytest.mark.asyncio
async def test_users_cannot_see_each_others_chats():
    """The isolation guarantee: each user's history is keyed separately."""
    storage = MemoryStorage()
    agent, _ = make_agent(ScriptedProvider([text_response("ok")]), storage)

    [e async for e in agent.send("alice", "alice-secret")]
    [e async for e in agent.send("bob", "bob-secret")]

    alice = str((await agent.load_session("alice")).to_dict())
    bob = str((await agent.load_session("bob")).to_dict())

    assert "alice-secret" in alice and "bob-secret" not in alice
    assert "bob-secret" in bob and "alice-secret" not in bob


@pytest.mark.asyncio
async def test_pending_approval_survives_instance_swap():
    """A parked write must be resumable from a different instance."""
    storage = MemoryStorage()

    first, _ = make_agent(
        ScriptedProvider([call_response("notion-update-page", {"page_id": "p1"})]),
        storage,
    )
    events = [e async for e in first.send("alice", "edit it")]
    call_id = next(e["call_id"] for e in events if e["type"] == "approval_required")

    second, _ = make_agent(ScriptedProvider([text_response("Done.")]), storage)
    session = await second.load_session("alice")

    assert call_id in session.pending
    pending = session.pending[call_id]
    assert pending.tool_name == "notion-update-page"
    assert pending.arguments == {"page_id": "p1"}


@pytest.mark.asyncio
async def test_file_storage_roundtrip_and_ttl(tmp_path):
    storage = FileStorage(tmp_path)

    await storage.set("k", {"a": 1})
    assert await storage.get("k") == {"a": 1}

    await storage.delete("k")
    assert await storage.get("k") is None

    # An already-elapsed TTL must read back as absent.
    await storage.set("expiring", "v", ttl_seconds=-1)
    assert await storage.get("expiring") is None


@pytest.mark.asyncio
async def test_file_storage_persists_across_objects(tmp_path):
    await FileStorage(tmp_path).set("k", "v")
    assert await FileStorage(tmp_path).get("k") == "v"
