"""Chat + approval routes, streamed to the browser over SSE."""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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


class ApproveRequest(BaseModel):
    call_id: str
    decision: Literal["approve", "reject"]


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


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    events = state.agent.send(user_id, payload.message)
    return StreamingResponse(
        _stream(events), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.post("/approve")
async def approve(
    payload: ApproveRequest,
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    events = state.agent.resume(user_id, payload.call_id, payload.decision)
    return StreamingResponse(
        _stream(events), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.post("/reset")
async def reset(
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    state.agent.reset(user_id)
    return {"ok": True}
