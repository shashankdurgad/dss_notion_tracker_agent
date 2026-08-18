"""Transient Gemini failures (503/429) must retry, then fail cleanly."""

from __future__ import annotations

import pytest
from google.genai import types

from backend.agent import _friendly_model_error, _is_retryable
from tests.test_approval_gate import FakeMCP, _agent, _text


class Boom(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class FlakyModels:
    """Fails `failures` times, then succeeds."""

    def __init__(self, failures: int, exc: Exception) -> None:
        self.failures = failures
        self.exc = exc
        self.attempts = 0

    async def generate_content(self, **kwargs):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.exc
        return type(
            "Resp",
            (),
            {"candidates": [type("C", (), {"content": _text("Recovered.")})()]},
        )()


def _with_models(agent, models) -> None:
    agent._client = type(
        "C", (), {"aio": type("A", (), {"models": models})()}
    )()


def test_retryable_classification():
    assert _is_retryable(Boom("503 UNAVAILABLE", 503))
    assert _is_retryable(Boom("429 RESOURCE_EXHAUSTED", 429))
    assert _is_retryable(Boom("This model is currently experiencing high demand"))
    # Client errors must NOT be retried — retrying a bad key just wastes time.
    assert not _is_retryable(Boom("400 INVALID_ARGUMENT", 400))
    assert not _is_retryable(Boom("API key not valid", 401))


def test_friendly_messages():
    assert "capacity" in _friendly_model_error(Boom("503 UNAVAILABLE")).lower()
    assert "rate limit" in _friendly_model_error(Boom("429")).lower()
    assert "GEMINI_API_KEY" in _friendly_model_error(Boom("API key not valid"))


@pytest.mark.asyncio
async def test_recovers_after_transient_503(monkeypatch):
    monkeypatch.setattr("backend.agent.asyncio.sleep", lambda _: _noop())
    agent, _ = _agent(monkeypatch, [_text("unused")])
    flaky = FlakyModels(2, Boom("503 UNAVAILABLE", 503))
    _with_models(agent, flaky)

    events = [e async for e in agent.send("u1", "hello")]

    assert flaky.attempts == 3  # two failures, then success
    assert any(e["type"] == "message" and "Recovered" in e["text"] for e in events)
    assert not any(e["type"] == "error" for e in events)


@pytest.mark.asyncio
async def test_gives_up_and_rewinds_history(monkeypatch):
    monkeypatch.setattr("backend.agent.asyncio.sleep", lambda _: _noop())
    agent, _ = _agent(monkeypatch, [_text("unused")])
    _with_models(agent, FlakyModels(99, Boom("503 UNAVAILABLE", 503)))

    events = [e async for e in agent.send("u1", "hello")]

    assert events[-1]["type"] == "error"
    assert "capacity" in events[-1]["message"].lower()
    # The rewind must be *persisted*, not just applied in memory — otherwise
    # the next request reloads a history ending in an unanswered user turn.
    history = (await agent.load_session("u1")).history
    assert all(c.role == "model" for c in history[-1:]) or history == []


@pytest.mark.asyncio
async def test_client_error_not_retried(monkeypatch):
    monkeypatch.setattr("backend.agent.asyncio.sleep", lambda _: _noop())
    agent, _ = _agent(monkeypatch, [_text("unused")])
    flaky = FlakyModels(99, Boom("400 INVALID_ARGUMENT", 400))
    _with_models(agent, flaky)

    events = [e async for e in agent.send("u1", "hello")]

    assert flaky.attempts == 1  # no retries
    assert events[-1]["type"] == "error"


async def _noop() -> None:
    return None
