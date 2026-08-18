"""Saved conversations: a per-user index plus one record per chat.

Storage layout (all keyed by user, never shared between users):

    chats:<user_id>              -> [ConversationMeta, ...]  newest first
    chat:<user_id>:<chat_id>     -> ChatSession

The index is kept separate from the messages so the sidebar can be rendered
with one small read, instead of loading every conversation's full history.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Fallback when a generated title isn't available.
UNTITLED = "New chat"


@dataclass
class ConversationMeta:
    id: str
    title: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationMeta:
        return cls(
            id=data["id"],
            title=data.get("title") or UNTITLED,
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
        )


def new_conversation_id() -> str:
    return uuid.uuid4().hex


def fallback_title(message: str) -> str:
    """Title from the user's first message, for when generation is unavailable."""
    cleaned = " ".join(message.split())
    if len(cleaned) <= 60:
        return cleaned or UNTITLED
    return cleaned[:57].rstrip() + "…"


class ConversationIndex:
    """CRUD over the per-user list of saved conversations."""

    def __init__(self, storage: Any, ttl_seconds: int, max_conversations: int) -> None:
        self._storage = storage
        self._ttl = ttl_seconds
        self._max = max_conversations

    @staticmethod
    def _key(user_id: str) -> str:
        return f"chats:{user_id}"

    async def list(self, user_id: str) -> list[ConversationMeta]:
        raw = await self._storage.get(self._key(user_id))
        if not isinstance(raw, list):
            return []
        items: list[ConversationMeta] = []
        for entry in raw:
            try:
                items.append(ConversationMeta.from_dict(entry))
            except (KeyError, TypeError):
                continue  # skip malformed rows rather than losing the list
        items.sort(key=lambda c: c.updated_at, reverse=True)
        return items

    async def _write(self, user_id: str, items: list[ConversationMeta]) -> None:
        await self._storage.set(
            self._key(user_id),
            [c.to_dict() for c in items],
            ttl_seconds=self._ttl,
        )

    async def touch(
        self, user_id: str, conversation_id: str, *, title: str | None = None
    ) -> list[str]:
        """Create or update an entry, returning ids evicted past the cap."""
        items = await self.list(user_id)
        now = time.time()

        for item in items:
            if item.id == conversation_id:
                item.updated_at = now
                if title:
                    item.title = title
                break
        else:
            items.append(
                ConversationMeta(
                    id=conversation_id,
                    title=title or UNTITLED,
                    created_at=now,
                    updated_at=now,
                )
            )

        items.sort(key=lambda c: c.updated_at, reverse=True)
        evicted = [c.id for c in items[self._max :]]
        items = items[: self._max]

        await self._write(user_id, items)
        return evicted

    async def rename(self, user_id: str, conversation_id: str, title: str) -> bool:
        items = await self.list(user_id)
        for item in items:
            if item.id == conversation_id:
                item.title = title.strip() or UNTITLED
                await self._write(user_id, items)
                return True
        return False

    async def remove(self, user_id: str, conversation_id: str) -> None:
        items = [c for c in await self.list(user_id) if c.id != conversation_id]
        await self._write(user_id, items)

    async def clear(self, user_id: str) -> list[str]:
        """Drop every conversation, returning the ids so callers can delete them."""
        ids = [c.id for c in await self.list(user_id)]
        await self._storage.delete(self._key(user_id))
        return ids
