"""Message conversion between the neutral format and each provider's wire format.

Round-tripping matters because chat history is persisted, so a lossy
conversion corrupts conversations rather than just one request.
"""

from __future__ import annotations

import json

import pytest

from backend.llm import (
    Message,
    OpenRouterProvider,
    ToolCall,
    _to_openai,
    build_provider,
)
from tests.conftest import make_settings


def test_message_roundtrip_through_storage():
    original = Message(
        role="assistant",
        text="Looking that up.",
        tool_calls=[ToolCall(id="c1", name="notion-search", arguments={"query": "x"})],
    )
    restored = Message.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored.role == "assistant"
    assert restored.text == original.text
    assert restored.tool_calls[0].name == "notion-search"
    assert restored.tool_calls[0].arguments == {"query": "x"}


def test_openai_tool_call_serialization():
    """OpenAI expects tool arguments as a JSON *string*, not an object."""
    msg = Message(
        role="assistant",
        tool_calls=[ToolCall(id="c1", name="notion-fetch", arguments={"id": "p1"})],
    )
    wire = _to_openai(msg)

    assert wire["role"] == "assistant"
    args = wire["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, str), "arguments must be a JSON string on the wire"
    assert json.loads(args) == {"id": "p1"}


def test_openai_tool_result_shape():
    msg = Message(
        role="tool", text="ok", tool_call_id="c1", tool_name="notion-fetch"
    )
    wire = _to_openai(msg)

    # Results must carry tool_call_id so the model can match them to the call.
    assert wire == {"role": "tool", "tool_call_id": "c1", "content": "ok"}


@pytest.mark.asyncio
async def test_openrouter_parses_string_arguments(monkeypatch):
    """Tool arguments come back as a JSON string and must be parsed to a dict."""
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_abc",
                                    "function": {
                                        "name": "notion-search",
                                        "arguments": '{"query": "minutes"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, body=json)
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

    provider = OpenRouterProvider("key", "some/model:free")
    result = await provider.generate(
        system="sys", messages=[Message(role="user", text="hi")], tools=[]
    )

    assert result.tool_calls[0].name == "notion-search"
    assert result.tool_calls[0].arguments == {"query": "minutes"}
    assert captured["headers"]["Authorization"] == "Bearer key"
    # System prompt must be the first message for OpenAI-compatible APIs.
    assert captured["body"]["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_openrouter_raises_on_error_body(monkeypatch):
    """An error nested inside a 200 response must not look like an empty reply."""

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"error": {"message": "No endpoints found that support tool use"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

    provider = OpenRouterProvider("key", "some/model:free")
    with pytest.raises(RuntimeError, match="tool use"):
        await provider.generate(system="s", messages=[], tools=[])


def test_build_provider_selects_backend():
    settings = make_settings()
    assert build_provider(settings).name == "gemini"

    settings.llm_provider = "openrouter"
    settings.openrouter_api_key = "sk-or-test"
    assert build_provider(settings).name == "openrouter"


def test_openrouter_default_model_is_the_verified_one():
    """Pin the default. Free models vary wildly at tool calling, and this one
    is the only one verified end-to-end against the real system prompt."""
    assert (
        make_settings().openrouter_model
        == "nvidia/nemotron-3-super-120b-a12b:free"
    )


def test_provider_validation_rejects_missing_key():
    settings = make_settings()
    settings.llm_provider = "openrouter"
    settings.openrouter_api_key = ""
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        settings.validate_provider()

    settings.llm_provider = "nonsense"
    with pytest.raises(ValueError, match="must be"):
        settings.validate_provider()
