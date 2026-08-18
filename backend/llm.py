"""Provider-neutral LLM interface.

The agent loop needs three things from a model: send a conversation plus tool
definitions, get back either text or tool calls, and feed tool results in. Both
Gemini and OpenAI-compatible APIs (OpenRouter) can do that, but they disagree
on wire format, so the conversation is kept in a neutral shape here and
converted per provider.

Keeping the neutral shape as plain dicts matters for a second reason: chat
history is serialized into Redis, so it must not be tied to one SDK's types.
Switching providers mid-conversation therefore doesn't corrupt stored history.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

Role = Literal["user", "assistant", "tool"]


@dataclass
class ToolCall:
    """A model's request to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        return cls(
            id=data.get("id", ""),
            name=data["name"],
            arguments=data.get("arguments") or {},
        )


@dataclass
class Message:
    """One conversation turn in provider-neutral form."""

    role: Role
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Set on role="tool" messages, linking a result back to its call.
    tool_call_id: str = ""
    tool_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role}
        if self.text:
            data["text"] = self.text
        if self.tool_calls:
            data["tool_calls"] = [c.to_dict() for c in self.tool_calls]
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.tool_name:
            data["tool_name"] = self.tool_name
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            role=data["role"],
            text=data.get("text", ""),
            tool_calls=[ToolCall.from_dict(c) for c in data.get("tool_calls", [])],
            tool_call_id=data.get("tool_call_id", ""),
            tool_name=data.get("tool_name", ""),
        )


@dataclass
class ToolSpec:
    """A tool the model may call, as a JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_message(self) -> Message:
        return Message(role="assistant", text=self.text, tool_calls=self.tool_calls)


class LLMProvider(Protocol):
    """What the agent loop needs from any model backend."""

    name: str

    async def generate(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse: ...


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def generate(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=t.parameters,
                        )
                        for t in tools
                    ]
                )
            ]
            if tools
            else None,
            # The approval gate depends on tools NOT executing automatically.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=[_to_gemini(m) for m in messages],
            config=config,
        )

        candidate = (response.candidates or [None])[0]
        if candidate is None or candidate.content is None:
            return LLMResponse()

        parts = candidate.content.parts or []
        text = "".join(p.text for p in parts if p.text).strip()
        calls = [
            ToolCall(
                id=getattr(p.function_call, "id", "") or f"call_{i}",
                name=p.function_call.name or "",
                arguments=dict(p.function_call.args or {}),
            )
            for i, p in enumerate(parts)
            if p.function_call
        ]
        return LLMResponse(text=text, tool_calls=calls)


def _to_gemini(message: Message):
    from google.genai import types

    if message.role == "tool":
        return types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name=message.tool_name, response={"result": message.text}
                    )
                )
            ],
        )

    parts: list[Any] = []
    if message.text:
        parts.append(types.Part(text=message.text))
    for call in message.tool_calls:
        parts.append(
            types.Part(
                function_call=types.FunctionCall(name=call.name, args=call.arguments)
            )
        )
    return types.Content(
        role="model" if message.role == "assistant" else "user",
        parts=parts or [types.Part(text="")],
    )


# --------------------------------------------------------------------------
# OpenRouter (OpenAI-compatible)
# --------------------------------------------------------------------------

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self, api_key: str, model: str, *, referer: str = "", title: str = ""
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._referer = referer
        self._title = title

    async def generate(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        import httpx

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}]
            + [_to_openai(m) for m in messages],
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        headers = {"Authorization": f"Bearer {self._api_key}"}
        # OpenRouter uses these for attribution on its dashboards; harmless
        # if unset, but nice to identify the app.
        if self._referer:
            headers["HTTP-Referer"] = self._referer
        if self._title:
            headers["X-Title"] = self._title

        async with httpx.AsyncClient(timeout=120.0) as http:
            response = await http.post(OPENROUTER_URL, headers=headers, json=payload)

        if response.status_code != 200:
            raise RuntimeError(
                f"OpenRouter error {response.status_code}: {response.text[:400]}"
            )

        body = response.json()
        # OpenRouter can return an error object inside a 200 response.
        if "error" in body and not body.get("choices"):
            raise RuntimeError(f"OpenRouter error: {json.dumps(body['error'])[:400]}")

        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}

        calls: list[ToolCall] = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args = fn.get("arguments")
            # Arguments arrive as a JSON *string* on the OpenAI wire format.
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    logger.warning("Unparseable tool arguments: %r", args[:200])
                    args = {}
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{len(calls)}",
                    name=fn.get("name") or "",
                    arguments=args if isinstance(args, dict) else {},
                )
            )

        return LLMResponse(text=(msg.get("content") or "").strip(), tool_calls=calls)


def _to_openai(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.text,
        }

    if message.role == "assistant":
        data: dict[str, Any] = {"role": "assistant", "content": message.text or None}
        if message.tool_calls:
            data["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps(c.arguments),
                    },
                }
                for c in message.tool_calls
            ]
        return data

    return {"role": "user", "content": message.text}


def build_provider(settings: Any) -> LLMProvider:
    if settings.llm_provider == "openrouter":
        if not settings.openrouter_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set."
            )
        return OpenRouterProvider(
            settings.openrouter_api_key,
            settings.openrouter_model,
            referer=settings.frontend_url,
            title="DSS Notion Assistant",
        )
    return GeminiProvider(settings.gemini_api_key, settings.gemini_model)
