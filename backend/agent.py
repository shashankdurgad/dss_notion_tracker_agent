"""Gemini agent loop over Notion MCP tools, with a write-approval gate.

Automatic function calling is deliberately DISABLED. If the SDK executed tools
for us, a page-write would land before we could ask the user. Instead we run
the loop by hand: reads execute immediately, writes suspend the turn and wait
for an explicit approval from the UI.

Verified against google-genai==2.18.1 and mcp==2.0.0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

from google import genai
from google.genai import types
from mcp.types import Tool as MCPTool

from .config import Settings
from .mcp_client import NotionMCPManager

logger = logging.getLogger(__name__)

# Tools that mutate the workspace. Everything else is treated as read-only.
# Kept as an explicit allowlist of *writes* so a newly-introduced Notion tool
# defaults to requiring approval only if we add it here — see _is_write().
WRITE_TOOLS = {
    "notion-create-pages",
    "notion-update-page",
    "notion-create-comment",
    "notion-create-database",
    "notion-update-database",
    "notion-create-file-upload",
    "notion-move-pages",
    "notion-duplicate-page",
}

# Any tool whose name contains one of these is also treated as a write, so a
# renamed or newly added mutating tool fails safe rather than running silently.
WRITE_HINTS = ("create", "update", "delete", "append", "move", "duplicate", "upload")

SYSTEM_INSTRUCTION = """\
You are the UCL Data Science Society (DSS) internal assistant. You answer \
questions about the society's Notion workspace — committee minutes, event \
planning, sponsorship threads, action items and task tracking — and you can \
create or update pages when asked.

Rules:
- Ground every factual claim in content you actually retrieved from Notion via \
the tools. Never invent page contents, decisions, dates, names or numbers.
- When you reference a page, include its Notion URL so the user can open it.
- If a search returns nothing relevant, say so plainly and suggest a better \
query rather than guessing.
- Prefer searching first to locate the right page, then fetching it for detail.
- Be concise. Committee members are usually skimming for one specific fact.
- For writes (creating or editing pages, posting comments), state clearly what \
you are about to change. The user must approve it before it happens.
"""


def _is_write(tool_name: str) -> bool:
    if tool_name in WRITE_TOOLS:
        return True
    lowered = tool_name.lower()
    return any(hint in lowered for hint in WRITE_HINTS)


# Gemini capacity errors. 503 = model saturated, 429 = rate limited,
# 500/504 = transient server-side. All are worth retrying; 400-class errors
# (bad request, bad key, quota exhausted) are not.
_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
_MAX_MODEL_ATTEMPTS = 4
_BASE_BACKOFF_SECONDS = 1.0


def _is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _RETRYABLE_STATUSES:
        return True
    text = str(exc)
    if any(str(status) in text for status in _RETRYABLE_STATUSES):
        return True
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "unavailable",
            "resource_exhausted",
            "high demand",
            "overloaded",
            "try again later",
            "deadline exceeded",
        )
    )


def _friendly_model_error(exc: Exception) -> str:
    text = str(exc)
    if "503" in text or "UNAVAILABLE" in text:
        return (
            "Gemini is at capacity right now, and it stayed busy across several "
            "retries. Your message wasn't lost — send it again in a moment."
        )
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return (
            "Hit the Gemini rate limit. Wait a few seconds and try again; if it "
            "keeps happening, check your API quota."
        )
    if "API key" in text or "401" in text or "403" in text:
        return "Gemini rejected the API key. Check GEMINI_API_KEY in .env."
    return f"The model call failed: {exc}"


@dataclass
class PendingWrite:
    """A write the model requested, parked until the user decides."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    # Full conversation up to and including the model turn that asked for this.
    history: list[types.Content]
    # Other calls in the same model turn that already ran (reads).
    completed_parts: list[types.Part] = field(default_factory=list)


@dataclass
class ChatSession:
    """Per-user conversation state.

    Persisted to shared storage between requests, so a conversation survives
    across serverless instances. Keyed by user; never shared between users.
    """

    history: list[types.Content] = field(default_factory=list)
    pending: dict[str, PendingWrite] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "history": [c.model_dump(mode="json", exclude_none=True) for c in self.history],
            "pending": {
                call_id: {
                    "call_id": p.call_id,
                    "tool_name": p.tool_name,
                    "arguments": p.arguments,
                    "history": [
                        c.model_dump(mode="json", exclude_none=True) for c in p.history
                    ],
                    "completed_parts": [
                        part.model_dump(mode="json", exclude_none=True)
                        for part in p.completed_parts
                    ],
                }
                for call_id, p in self.pending.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatSession:
        return cls(
            history=[types.Content(**c) for c in data.get("history", [])],
            pending={
                call_id: PendingWrite(
                    call_id=p["call_id"],
                    tool_name=p["tool_name"],
                    arguments=p["arguments"],
                    history=[types.Content(**c) for c in p["history"]],
                    completed_parts=[types.Part(**part) for part in p["completed_parts"]],
                )
                for call_id, p in (data.get("pending") or {}).items()
            },
        )


AgentEvent = dict[str, Any]


class NotionAgent:
    def __init__(
        self, settings: Settings, mcp: NotionMCPManager, storage: Any
    ) -> None:
        self._settings = settings
        self._mcp = mcp
        self._storage = storage
        self._client = genai.Client(api_key=settings.gemini_api_key)

    @staticmethod
    def _key(user_id: str) -> str:
        return f"chat:{user_id}"

    async def load_session(self, user_id: str) -> ChatSession:
        raw = await self._storage.get(self._key(user_id))
        if isinstance(raw, dict):
            try:
                return ChatSession.from_dict(raw)
            except Exception:  # noqa: BLE001 - shape drift shouldn't wedge chat
                logger.warning("Discarding unreadable chat session", exc_info=True)
        return ChatSession()

    async def save_session(self, user_id: str, session: ChatSession) -> None:
        await self._storage.set(
            self._key(user_id),
            session.to_dict(),
            ttl_seconds=self._settings.chat_ttl_seconds,
        )

    async def reset(self, user_id: str) -> None:
        await self._storage.delete(self._key(user_id))

    # ---------- tool plumbing ----------

    async def _tool_config(self, user_id: str) -> list[types.Tool]:
        mcp_tools = await self._mcp.list_tools(user_id)
        declarations = [self._declare(tool) for tool in mcp_tools]
        return [types.Tool(function_declarations=declarations)]

    @staticmethod
    def _declare(tool: MCPTool) -> types.FunctionDeclaration:
        # Pass the MCP JSON Schema through verbatim. parameters_json_schema
        # accepts raw JSON Schema, so there's no lossy hand-conversion and the
        # declarations stay correct as Notion's Beta tool shapes change.
        return types.FunctionDeclaration(
            name=tool.name,
            description=tool.description or "",
            parameters_json_schema=tool.input_schema or {"type": "object"},
        )

    async def _execute(self, user_id: str, name: str, args: dict[str, Any]) -> str:
        result = await self._mcp.call_tool(user_id, name, args)
        return _result_to_text(result)

    # ---------- main loop ----------

    async def send(self, user_id: str, message: str) -> AsyncIterator[AgentEvent]:
        session = await self.load_session(user_id)
        session.history.append(
            types.Content(role="user", parts=[types.Part(text=message)])
        )
        try:
            async for event in self._run(user_id, session):
                yield event
        finally:
            # Persist even on error or client disconnect, so the rewound
            # history and any parked approval survive to the next request.
            await self.save_session(user_id, session)

    async def resume(
        self,
        user_id: str,
        call_id: str,
        decision: Literal["approve", "reject"],
    ) -> AsyncIterator[AgentEvent]:
        session = await self.load_session(user_id)
        pending = session.pending.pop(call_id, None)
        if pending is None:
            yield {"type": "error", "message": "That approval request has expired."}
            return

        session.history = pending.history
        parts = list(pending.completed_parts)

        if decision == "approve":
            yield {
                "type": "tool_start",
                "tool": pending.tool_name,
                "arguments": pending.arguments,
            }
            try:
                output = await self._execute(
                    user_id, pending.tool_name, pending.arguments
                )
            except Exception as exc:  # noqa: BLE001 - fed back to the model
                output = f"Tool failed: {exc}"
                logger.exception("Approved write failed")
            yield {"type": "tool_result", "tool": pending.tool_name, "result": output}
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=pending.tool_name, response={"result": output}
                    )
                )
            )
        else:
            # Tell the model it was declined so it can respond gracefully
            # instead of silently assuming the write succeeded.
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=pending.tool_name,
                        response={
                            "result": "The user declined this change. "
                            "It was NOT applied to Notion."
                        },
                    )
                )
            )
            yield {"type": "tool_rejected", "tool": pending.tool_name}

        session.history.append(types.Content(role="user", parts=parts))
        try:
            async for event in self._run(user_id, session):
                yield event
        finally:
            await self.save_session(user_id, session)

    async def _generate(
        self, session: ChatSession, config: types.GenerateContentConfig
    ):
        """Call Gemini, retrying transient capacity errors with backoff."""
        last: Exception | None = None
        for attempt in range(_MAX_MODEL_ATTEMPTS):
            try:
                return await self._client.aio.models.generate_content(
                    model=self._settings.gemini_model,
                    contents=session.history,
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                if not _is_retryable(exc) or attempt == _MAX_MODEL_ATTEMPTS - 1:
                    raise
                # Exponential backoff with jitter: ~1s, 2s, 4s. Jitter stops
                # several users' retries from landing in lockstep.
                delay = _BASE_BACKOFF_SECONDS * (2**attempt)
                delay *= 0.5 + random.random()
                logger.info(
                    "Gemini unavailable (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    _MAX_MODEL_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
        raise last  # type: ignore[misc]

    @staticmethod
    def _rewind(session: ChatSession) -> None:
        """Drop trailing turns the model never answered.

        Leaving a user turn with no model reply corrupts the next request, so
        after a failure we trim back to the last model turn.
        """
        while session.history and session.history[-1].role != "model":
            session.history.pop()

    async def _run(
        self, user_id: str, session: ChatSession
    ) -> AsyncIterator[AgentEvent]:
        tools = await self._tool_config(user_id)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=tools,
            # The whole approval design depends on this being disabled.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

        for _ in range(self._settings.max_agent_iterations):
            try:
                response = await self._generate(session, config)
            except Exception as exc:  # noqa: BLE001 - reported to the user
                logger.warning("Gemini call failed: %s", exc)
                # Roll back to the last turn that completed cleanly, so a failed
                # attempt doesn't leave a dangling user turn with no reply. The
                # user can retry against intact history.
                self._rewind(session)
                yield {"type": "error", "message": _friendly_model_error(exc)}
                return

            candidate = (response.candidates or [None])[0]
            if candidate is None or candidate.content is None:
                self._rewind(session)
                yield {
                    "type": "error",
                    "message": "Gemini returned an empty response. Try again.",
                }
                return

            model_content = candidate.content
            session.history.append(model_content)

            calls = [p.function_call for p in (model_content.parts or []) if p.function_call]

            text = "".join(
                p.text for p in (model_content.parts or []) if p.text
            ).strip()
            if text:
                yield {"type": "message", "text": text}

            if not calls:
                yield {"type": "done"}
                return

            reads = [c for c in calls if not _is_write(c.name or "")]
            writes = [c for c in calls if _is_write(c.name or "")]

            parts: list[types.Part] = []

            # Reads run concurrently — they're safe and often several per turn.
            if reads:
                for call in reads:
                    yield {
                        "type": "tool_start",
                        "tool": call.name,
                        "arguments": dict(call.args or {}),
                    }
                results = await asyncio.gather(
                    *(
                        self._execute(user_id, c.name or "", dict(c.args or {}))
                        for c in reads
                    ),
                    return_exceptions=True,
                )
                for call, outcome in zip(reads, results):
                    output = (
                        f"Tool failed: {outcome}"
                        if isinstance(outcome, BaseException)
                        else outcome
                    )
                    yield {
                        "type": "tool_result",
                        "tool": call.name,
                        "result": output,
                    }
                    parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=call.name or "", response={"result": output}
                            )
                        )
                    )

            if writes:
                # Park the first write and hand control to the user. Any further
                # writes in this turn are dropped; the model re-proposes them on
                # the next turn with the approved state in hand.
                call = writes[0]
                call_id = uuid.uuid4().hex
                session.pending[call_id] = PendingWrite(
                    call_id=call_id,
                    tool_name=call.name or "",
                    arguments=dict(call.args or {}),
                    history=list(session.history),
                    completed_parts=parts,
                )
                yield {
                    "type": "approval_required",
                    "call_id": call_id,
                    "tool": call.name,
                    "arguments": dict(call.args or {}),
                }
                return

            session.history.append(types.Content(role="user", parts=parts))

        yield {
            "type": "error",
            "message": "Stopped after too many tool steps. Try a narrower question.",
        }


def _result_to_text(result: Any) -> str:
    """Flatten an MCP CallToolResult into text for the model."""
    if getattr(result, "is_error", False):
        prefix = "ERROR: "
    else:
        prefix = ""

    structured = getattr(result, "structured_content", None)
    if structured:
        return prefix + json.dumps(structured, ensure_ascii=False)[:20000]

    chunks: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            chunks.append(text)
    if chunks:
        return prefix + "\n".join(chunks)[:20000]
    return prefix + "(no content returned)"
