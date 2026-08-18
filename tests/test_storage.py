"""State must survive across instances, and stay isolated between users.

These are the properties the Vercel/serverless refactor exists to guarantee.
"""

from __future__ import annotations

import pytest
from google.genai import types

from backend.agent import NotionAgent
from backend.storage import FileStorage
from tests.test_approval_gate import FakeMCP, MemoryStorage, _call, _text


def _make_agent(monkeypatch, storage, turns):
    from backend.config import Settings

    settings = Settings(
        gemini_api_key="test", session_secret="s" * 32, token_enc_key="k" * 32
    )
    monkeypatch.setattr(
        "backend.agent.genai.Client", lambda **_: type("C", (), {"aio": None})()
    )
    agent = NotionAgent(settings, FakeMCP(), storage)  # type: ignore[arg-type]

    class Models:
        def __init__(self) -> None:
            self.i = 0

        async def generate_content(self, **kwargs):
            content = turns[min(self.i, len(turns) - 1)]
            self.i += 1
            return type(
                "R", (), {"candidates": [type("C", (), {"content": content})()]}
            )()

    agent._client = type("C", (), {"aio": type("A", (), {"models": Models()})()})()
    return agent


@pytest.mark.asyncio
async def test_history_survives_instance_swap(monkeypatch):
    """A second 'instance' sharing storage sees the first one's conversation."""
    storage = MemoryStorage()

    first = _make_agent(monkeypatch, storage, [_text("Hello there.")])
    [e async for e in first.send("alice", "hi")]

    # Simulate a cold start: brand new agent object, same shared storage.
    second = _make_agent(monkeypatch, storage, [_text("Still here.")])
    session = await second.load_session("alice")

    assert len(session.history) == 2  # user turn + model reply
    assert session.history[0].parts[0].text == "hi"


@pytest.mark.asyncio
async def test_users_cannot_see_each_others_chats(monkeypatch):
    """The isolation guarantee: each user's history is keyed separately."""
    storage = MemoryStorage()
    agent = _make_agent(monkeypatch, storage, [_text("ok")])

    [e async for e in agent.send("alice", "alice-secret")]
    [e async for e in agent.send("bob", "bob-secret")]

    alice = await agent.load_session("alice")
    bob = await agent.load_session("bob")

    alice_text = str(alice.to_dict())
    bob_text = str(bob.to_dict())
    assert "alice-secret" in alice_text and "bob-secret" not in alice_text
    assert "bob-secret" in bob_text and "alice-secret" not in bob_text


@pytest.mark.asyncio
async def test_pending_approval_survives_instance_swap(monkeypatch):
    """A parked write must be resumable from a different instance."""
    storage = MemoryStorage()

    first = _make_agent(
        monkeypatch, storage, [_call("notion-update-page", {"page_id": "p1"})]
    )
    events = [e async for e in first.send("alice", "edit it")]
    call_id = next(e["call_id"] for e in events if e["type"] == "approval_required")

    second = _make_agent(monkeypatch, storage, [_text("Done.")])
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
