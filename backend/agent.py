"""Agent loop over Notion MCP tools, with a write-approval gate.

Tools are never executed automatically by the model SDK. If they were, a
page-write would land before we could ask the user. The loop is driven by
hand: reads execute immediately, writes suspend the turn and wait for an
explicit approval from the UI.

The loop is provider-agnostic (see llm.py) so it runs on Gemini or on any
OpenAI-compatible endpoint such as OpenRouter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

from mcp.types import Tool as MCPTool

from .config import Settings
from .llm import LLMProvider, Message, ToolCall, ToolSpec
from .mcp_client import NotionMCPManager

logger = logging.getLogger(__name__)

# Tools that mutate the workspace. Everything else is treated as read-only.
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


# Transient capacity errors. 503 = saturated, 429 = rate limited,
# 500/502/504 = transient server-side. Client errors are not retried.
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
            "rate limit",
        )
    )


def _friendly_model_error(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "503" in text or "unavailable" in lowered or "high demand" in lowered:
        return (
            "The model is at capacity right now, and stayed busy across several "
            "retries. Your message wasn't lost — send it again in a moment."
        )
    if "429" in text or "resource_exhausted" in lowered or "rate limit" in lowered:
        return (
            "Hit the model's rate limit. Wait a few seconds and try again; if it "
            "keeps happening you may be out of daily quota."
        )
    if "api key" in lowered or "401" in text or "403" in text:
        return "The model provider rejected the API key. Check your configuration."
    return f"The model call failed: {exc}"


@dataclass
class PendingWrite:
    """A write the model requested, parked until the user decides."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    # Conversation up to and including the model turn that asked for this.
    history: list[Message]
    # Results of other calls in the same turn that already ran (reads).
    completed: list[Message] = field(default_factory=list)
    # The provider-assigned id, needed to match the result back to the call.
    provider_call_id: str = ""


@dataclass
class ChatSession:
    """Per-user conversation state, persisted between requests."""

    history: list[Message] = field(default_factory=list)
    pending: dict[str, PendingWrite] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "history": [m.to_dict() for m in self.history],
            "pending": {
                call_id: {
                    "call_id": p.call_id,
                    "tool_name": p.tool_name,
                    "arguments": p.arguments,
                    "provider_call_id": p.provider_call_id,
                    "history": [m.to_dict() for m in p.history],
                    "completed": [m.to_dict() for m in p.completed],
                }
                for call_id, p in self.pending.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatSession:
        return cls(
            history=[Message.from_dict(m) for m in data.get("history", [])],
            pending={
                call_id: PendingWrite(
                    call_id=p["call_id"],
                    tool_name=p["tool_name"],
                    arguments=p["arguments"],
                    provider_call_id=p.get("provider_call_id", ""),
                    history=[Message.from_dict(m) for m in p["history"]],
                    completed=[Message.from_dict(m) for m in p.get("completed", [])],
                )
                for call_id, p in (data.get("pending") or {}).items()
            },
        )


AgentEvent = dict[str, Any]


class NotionAgent:
    def __init__(
        self,
        settings: Settings,
        mcp: NotionMCPManager,
        storage: Any,
        provider: LLMProvider,
    ) -> None:
        self._settings = settings
        self._mcp = mcp
        self._storage = storage
        self._provider = provider

    @staticmethod
    def _key(user_id: str, conversation_id: str) -> str:
        return f"chat:{user_id}:{conversation_id}"

    async def load_session(
        self, user_id: str, conversation_id: str
    ) -> ChatSession:
        raw = await self._storage.get(self._key(user_id, conversation_id))
        if isinstance(raw, dict):
            try:
                return ChatSession.from_dict(raw)
            except Exception:  # noqa: BLE001 - shape drift shouldn't wedge chat
                logger.warning("Discarding unreadable chat session", exc_info=True)
        return ChatSession()

    async def save_session(
        self, user_id: str, conversation_id: str, session: ChatSession
    ) -> None:
        await self._storage.set(
            self._key(user_id, conversation_id),
            session.to_dict(),
            ttl_seconds=self._settings.chat_ttl_seconds,
        )

    async def reset(self, user_id: str, conversation_id: str) -> None:
        await self._storage.delete(self._key(user_id, conversation_id))

    async def generate_title(self, first_message: str) -> str:
        """Ask the model for a short title for the sidebar.

        Best-effort: the caller falls back to the user's first message. Uses
        no tools and caps output, so it stays a cheap single request — this
        matters on a free tier with a daily request cap.
        """
        try:
            response = await self._provider.generate(
                system=(
                    "Write a 3-6 word title summarising the user's request, "
                    "for a chat sidebar. Reply with the title only: no quotes, "
                    "no punctuation at the end, no preamble."
                ),
                messages=[Message(role="user", text=first_message[:500])],
                tools=[],
            )
        except Exception:  # noqa: BLE001 - never let titling break a chat
            logger.info("Title generation failed; using fallback", exc_info=True)
            return ""
        title = " ".join((response.text or "").split()).strip('"\'' )
        # A rambling reply means the model ignored the instruction; don't use it.
        return title[:60] if 0 < len(title) <= 80 else ""

    # ---------- tool plumbing ----------

    async def _tools(self, user_id: str) -> list[ToolSpec]:
        return [self._spec(t) for t in await self._mcp.list_tools(user_id)]

    @staticmethod
    def _spec(tool: MCPTool) -> ToolSpec:
        # MCP schemas are JSON Schema already; pass through verbatim so the
        # declarations stay correct as Notion's Beta tool shapes change.
        return ToolSpec(
            name=tool.name,
            description=tool.description or "",
            parameters=tool.input_schema or {"type": "object"},
        )

    async def _execute(self, user_id: str, name: str, args: dict[str, Any]) -> str:
        result = await self._mcp.call_tool(user_id, name, args)
        return _result_to_text(result)

    @staticmethod
    def _tool_result(call: ToolCall, output: str) -> Message:
        return Message(
            role="tool",
            text=output,
            tool_call_id=call.id,
            tool_name=call.name,
        )

    # ---------- main loop ----------

    async def send(
        self, user_id: str, conversation_id: str, message: str
    ) -> AsyncIterator[AgentEvent]:
        session = await self.load_session(user_id, conversation_id)
        session.history.append(Message(role="user", text=message))
        try:
            async for event in self._run(user_id, session):
                yield event
        finally:
            # Persist even on error or client disconnect, so the rewound
            # history and any parked approval survive to the next request.
            await self.save_session(user_id, conversation_id, session)

    async def resume(
        self,
        user_id: str,
        conversation_id: str,
        call_id: str,
        decision: Literal["approve", "reject"],
    ) -> AsyncIterator[AgentEvent]:
        session = await self.load_session(user_id, conversation_id)
        pending = session.pending.pop(call_id, None)
        if pending is None:
            yield {"type": "error", "message": "That approval request has expired."}
            return

        session.history = pending.history
        results = list(pending.completed)
        call = ToolCall(
            id=pending.provider_call_id,
            name=pending.tool_name,
            arguments=pending.arguments,
        )

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
            results.append(self._tool_result(call, output))
        else:
            # Tell the model it was declined so it responds gracefully instead
            # of assuming the write succeeded.
            results.append(
                self._tool_result(
                    call,
                    "The user declined this change. It was NOT applied to Notion.",
                )
            )
            yield {"type": "tool_rejected", "tool": pending.tool_name}

        session.history.extend(results)
        try:
            async for event in self._run(user_id, session):
                yield event
        finally:
            await self.save_session(user_id, conversation_id, session)

    async def _generate(self, session: ChatSession, tools: list[ToolSpec]):
        """Call the model, retrying transient capacity errors with backoff."""
        last: Exception | None = None
        for attempt in range(_MAX_MODEL_ATTEMPTS):
            try:
                return await self._provider.generate(
                    system=SYSTEM_INSTRUCTION,
                    messages=session.history,
                    tools=tools,
                )
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                if not _is_retryable(exc) or attempt == _MAX_MODEL_ATTEMPTS - 1:
                    raise
                # Exponential backoff with jitter: ~1s, 2s, 4s. Jitter stops
                # several users' retries landing in lockstep.
                delay = _BASE_BACKOFF_SECONDS * (2**attempt)
                delay *= 0.5 + random.random()
                logger.info(
                    "Model unavailable (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    _MAX_MODEL_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
        raise last  # type: ignore[misc]

    @staticmethod
    def _rewind(session: ChatSession) -> None:
        """Drop trailing turns the model never answered.

        Leaving a user turn with no assistant reply corrupts the next request,
        so after a failure we trim back to the last assistant turn.
        """
        while session.history and session.history[-1].role != "assistant":
            session.history.pop()

    async def _run(
        self, user_id: str, session: ChatSession
    ) -> AsyncIterator[AgentEvent]:
        tools = await self._tools(user_id)

        for _ in range(self._settings.max_agent_iterations):
            try:
                response = await self._generate(session, tools)
            except Exception as exc:  # noqa: BLE001 - reported to the user
                logger.warning("Model call failed: %s", exc)
                self._rewind(session)
                yield {"type": "error", "message": _friendly_model_error(exc)}
                return

            if not response.text and not response.tool_calls:
                self._rewind(session)
                yield {
                    "type": "error",
                    "message": "The model returned an empty response. Try again.",
                }
                return

            session.history.append(response.as_message())

            if response.text:
                yield {"type": "message", "text": response.text}

            if not response.tool_calls:
                yield {"type": "done"}
                return

            reads = [c for c in response.tool_calls if not _is_write(c.name)]
            writes = [c for c in response.tool_calls if _is_write(c.name)]

            results: list[Message] = []

            # Reads run concurrently — they're safe and often several per turn.
            if reads:
                for call in reads:
                    yield {
                        "type": "tool_start",
                        "tool": call.name,
                        "arguments": call.arguments,
                    }
                outcomes = await asyncio.gather(
                    *(self._execute(user_id, c.name, c.arguments) for c in reads),
                    return_exceptions=True,
                )
                for call, outcome in zip(reads, outcomes):
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
                    results.append(self._tool_result(call, output))

            if writes:
                # Park the first write and hand control to the user. Further
                # writes this turn are dropped; the model re-proposes them next
                # turn with the approved state in hand.
                call = writes[0]
                call_id = uuid.uuid4().hex
                session.pending[call_id] = PendingWrite(
                    call_id=call_id,
                    tool_name=call.name,
                    arguments=call.arguments,
                    provider_call_id=call.id,
                    history=list(session.history),
                    completed=results,
                )
                yield {
                    "type": "approval_required",
                    "call_id": call_id,
                    "tool": call.name,
                    "arguments": call.arguments,
                }
                return

            session.history.extend(results)

        yield {
            "type": "error",
            "message": "Stopped after too many tool steps. Try a narrower question.",
        }


def _result_to_text(result: Any) -> str:
    """Flatten an MCP CallToolResult into text for the model."""
    prefix = "ERROR: " if getattr(result, "is_error", False) else ""

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
