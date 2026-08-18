"""Chat, approval and saved-conversation routes."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..conversations import fallback_title, new_conversation_id
from ..deps import AppState, current_user, get_state
from ..oauth import TerminalAuthError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Stops nginx buffering the stream and killing token-by-token delivery.
    "X-Accel-Buffering": "no",
}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None


class ApproveRequest(BaseModel):
    call_id: str
    decision: Literal["approve", "reject"]
    conversation_id: str


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _stream(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    """Wrap an agent event stream, turning auth failures into a re-login cue."""
    try:
        async for event in events:
            yield _sse(event)
    except TerminalAuthError as exc:
        yield _sse({"type": "auth_required", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        logger.exception("Chat stream failed")
        yield _sse({"type": "error", "message": f"Something went wrong: {exc}"})


# ---------------------------------------------------------------- chat


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    conversation_id = payload.conversation_id or new_conversation_id()
    is_new = payload.conversation_id is None

    # Register immediately with a cheap title so the sidebar updates even if
    # the user navigates away mid-reply.
    await state.conversations.touch(
        user_id,
        conversation_id,
        title=fallback_title(payload.message) if is_new else None,
    )

    async def events() -> AsyncIterator[dict]:
        # Tell the client which conversation this is, so a new chat can adopt
        # the server-generated id.
        yield {"type": "conversation", "conversation_id": conversation_id}
        async for event in state.agent.send(user_id, conversation_id, payload.message):
            yield event
        if is_new:
            # Generated after the reply, so titling never delays the answer.
            # It costs one extra model request, so only ever on the first turn.
            title = await state.agent.generate_title(payload.message)
            if title:
                await state.conversations.rename(user_id, conversation_id, title)
                yield {"type": "conversation_title", "title": title}

    return StreamingResponse(
        _stream(events()), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.post("/approve")
async def approve(
    payload: ApproveRequest,
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    events = state.agent.resume(
        user_id, payload.conversation_id, payload.call_id, payload.decision
    )
    return StreamingResponse(
        _stream(events), media_type="text/event-stream", headers=SSE_HEADERS
    )


# ------------------------------------------------------- conversations


@router.get("/conversations")
async def list_conversations(
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    items = await state.conversations.list(user_id)
    return {"conversations": [c.to_dict() for c in items]}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """Full message history, for rendering a chat the user switches back to."""
    session = await state.agent.load_session(user_id, conversation_id)
    messages = [
        {"role": m.role, "text": m.text}
        for m in session.history
        # Tool traffic isn't shown when replaying history: the tool chips are
        # live progress indicators, and raw tool JSON would be noise here.
        if m.role in ("user", "assistant") and m.text
    ]
    pending = [
        {
            "call_id": p.call_id,
            "tool": p.tool_name,
            "arguments": p.arguments,
        }
        for p in session.pending.values()
    ]
    return {"messages": messages, "pending": pending}


@router.post("/conversations/{conversation_id}/rename")
async def rename_conversation(
    conversation_id: str,
    payload: RenameRequest,
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    if not await state.conversations.rename(user_id, conversation_id, payload.title):
        raise HTTPException(status_code=404, detail="No such conversation.")
    return {"ok": True}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    await state.conversations.remove(user_id, conversation_id)
    await state.agent.reset(user_id, conversation_id)
    return {"ok": True}


@router.post("/conversations/clear")
async def clear_conversations(
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    ids = await state.conversations.clear(user_id)
    await asyncio.gather(*(state.agent.reset(user_id, cid) for cid in ids))
    return {"ok": True, "deleted": len(ids)}
