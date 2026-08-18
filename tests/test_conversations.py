"""Saved conversations: isolation, ordering, eviction and titling."""

from __future__ import annotations

import pytest

from backend.conversations import ConversationIndex, fallback_title
from backend.llm import LLMResponse
from tests.conftest import (
    MemoryStorage,
    ScriptedProvider,
    make_agent,
    text_response,
)


def index(storage, max_conversations=50) -> ConversationIndex:
    return ConversationIndex(storage, ttl_seconds=3600, max_conversations=max_conversations)


@pytest.mark.asyncio
async def test_conversations_are_listed_newest_first():
    idx = index(MemoryStorage())
    await idx.touch("alice", "a", title="First")
    await idx.touch("alice", "b", title="Second")
    await idx.touch("alice", "a")  # bump the older one

    items = await idx.list("alice")
    assert [c.id for c in items] == ["a", "b"]


@pytest.mark.asyncio
async def test_conversation_lists_are_per_user():
    storage = MemoryStorage()
    idx = index(storage)
    await idx.touch("alice", "a", title="Alice chat")
    await idx.touch("bob", "b", title="Bob chat")

    assert [c.id for c in await idx.list("alice")] == ["a"]
    assert [c.id for c in await idx.list("bob")] == ["b"]


@pytest.mark.asyncio
async def test_oldest_conversation_evicted_past_cap():
    idx = index(MemoryStorage(), max_conversations=2)
    await idx.touch("alice", "a")
    await idx.touch("alice", "b")
    evicted = await idx.touch("alice", "c")

    assert evicted == ["a"]
    assert {c.id for c in await idx.list("alice")} == {"b", "c"}


@pytest.mark.asyncio
async def test_rename_and_remove():
    idx = index(MemoryStorage())
    await idx.touch("alice", "a", title="Old")

    assert await idx.rename("alice", "a", "New title")
    assert (await idx.list("alice"))[0].title == "New title"
    assert not await idx.rename("alice", "missing", "x")

    await idx.remove("alice", "a")
    assert await idx.list("alice") == []


def test_fallback_title_truncates():
    assert fallback_title("Short question") == "Short question"
    long = "word " * 40
    assert len(fallback_title(long)) <= 60
    assert fallback_title(long).endswith("…")
    assert fallback_title("   ") == "New chat"


@pytest.mark.asyncio
async def test_separate_conversations_keep_separate_history():
    """The core promise: two chats for one user don't bleed into each other."""
    storage = MemoryStorage()
    agent, _ = make_agent(ScriptedProvider([text_response("ok")]), storage)

    [e async for e in agent.send("alice", "chat-1", "about the hackathon")]
    [e async for e in agent.send("alice", "chat-2", "about sponsorship")]

    one = await agent.load_session("alice", "chat-1")
    two = await agent.load_session("alice", "chat-2")

    assert "hackathon" in one.history[0].text
    assert "sponsorship" not in str(one.to_dict())
    assert "sponsorship" in two.history[0].text
    assert "hackathon" not in str(two.to_dict())


@pytest.mark.asyncio
async def test_deleting_one_conversation_leaves_others():
    storage = MemoryStorage()
    agent, _ = make_agent(ScriptedProvider([text_response("ok")]), storage)

    [e async for e in agent.send("alice", "chat-1", "first")]
    [e async for e in agent.send("alice", "chat-2", "second")]
    await agent.reset("alice", "chat-1")

    assert (await agent.load_session("alice", "chat-1")).history == []
    assert (await agent.load_session("alice", "chat-2")).history != []


@pytest.mark.asyncio
async def test_title_generation_and_fallback():
    agent, _ = make_agent(ScriptedProvider([LLMResponse(text="Hackathon Budget Decision")]))
    assert await agent.generate_title("what did we decide") == "Hackathon Budget Decision"

    # A rambling reply means the model ignored the instruction — reject it so
    # the caller falls back to the user's own words.
    agent, _ = make_agent(ScriptedProvider([LLMResponse(text="x " * 100)]))
    assert await agent.generate_title("q") == ""


@pytest.mark.asyncio
async def test_title_generation_never_raises():
    """Titling is cosmetic; a provider failure must not break the chat."""

    class Broken:
        name = "broken"

        async def generate(self, **kwargs):
            raise RuntimeError("429 rate limited")

    agent, _ = make_agent(Broken())
    assert await agent.generate_title("anything") == ""
