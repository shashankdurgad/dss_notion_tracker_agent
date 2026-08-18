"""Transient model failures (503/429) must retry, then fail cleanly."""

from __future__ import annotations

import pytest

from backend.agent import _friendly_model_error, _is_retryable
from tests.conftest import FailingProvider, make_agent, text_response


class Boom(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr("backend.agent.asyncio.sleep", instant)


def test_retryable_classification():
    assert _is_retryable(Boom("503 UNAVAILABLE", 503))
    assert _is_retryable(Boom("429 RESOURCE_EXHAUSTED", 429))
    assert _is_retryable(Boom("This model is currently experiencing high demand"))
    assert _is_retryable(Boom("Rate limit exceeded"))
    # Client errors must NOT be retried — retrying a bad key just wastes time.
    assert not _is_retryable(Boom("400 INVALID_ARGUMENT", 400))
    assert not _is_retryable(Boom("API key not valid", 401))


def test_friendly_messages():
    assert "capacity" in _friendly_model_error(Boom("503 UNAVAILABLE")).lower()
    assert "rate limit" in _friendly_model_error(Boom("429")).lower()
    assert "API key" in _friendly_model_error(Boom("API key not valid"))


@pytest.mark.asyncio
async def test_recovers_after_transient_503():
    provider = FailingProvider(2, Boom("503 UNAVAILABLE", 503), text_response("Recovered."))
    agent, _ = make_agent(provider)

    events = [e async for e in agent.send("u1", "c1", "hello")]

    assert provider.attempts == 3  # two failures, then success
    assert any(e["type"] == "message" and "Recovered" in e["text"] for e in events)
    assert not any(e["type"] == "error" for e in events)


@pytest.mark.asyncio
async def test_gives_up_and_rewinds_history():
    agent, _ = make_agent(
        FailingProvider(99, Boom("503 UNAVAILABLE", 503), text_response("never"))
    )

    events = [e async for e in agent.send("u1", "c1", "hello")]

    assert events[-1]["type"] == "error"
    assert "capacity" in events[-1]["message"].lower()
    # The rewind must be *persisted* — otherwise the next request reloads a
    # history ending in an unanswered user turn.
    history = (await agent.load_session("u1", "c1")).history
    assert all(m.role == "assistant" for m in history[-1:]) or history == []


@pytest.mark.asyncio
async def test_client_error_not_retried():
    provider = FailingProvider(
        99, Boom("400 INVALID_ARGUMENT", 400), text_response("never")
    )
    agent, _ = make_agent(provider)

    events = [e async for e in agent.send("u1", "c1", "hello")]

    assert provider.attempts == 1  # no retries
    assert events[-1]["type"] == "error"
