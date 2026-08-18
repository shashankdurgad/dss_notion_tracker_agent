"""Key-value storage with two backends.

Serverless functions are stateless: the filesystem is wiped between
invocations and consecutive requests from one user may land on different
instances. Anything that must outlive a single request therefore lives here.

- RedisStorage  — for Vercel/serverless. Shared across instances.
- FileStorage   — for local development. No external dependency.

"Shared" means shared between *server instances*, never between users: every
key is namespaced by the caller (session id, user id), so one user's cookie
can only ever address their own records. Values holding Notion tokens are
additionally encrypted before they get here.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Protocol


class Storage(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...


class FileStorage:
    """JSON file per namespace. Development only — not safe across processes."""

    def __init__(self, directory: Path) -> None:
        self._path = directory / "storage.json"
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._data = json.loads(self._path.read_text())
        except (ValueError, OSError):
            self._data = {}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write must not truncate the store and
        # strand a rotated refresh token.
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._data, indent=2))
        temp.chmod(0o600)
        temp.replace(self._path)

    def _expired(self, entry: dict[str, Any]) -> bool:
        expires = entry.get("expires_at")
        return expires is not None and time.time() >= expires

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if self._expired(entry):
                self._data.pop(key, None)
                self._flush()
                return None
            return entry["value"]

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        async with self._lock:
            self._data[key] = {
                "value": value,
                "expires_at": time.time() + ttl_seconds if ttl_seconds else None,
            }
            self._flush()

    async def delete(self, key: str) -> None:
        async with self._lock:
            if self._data.pop(key, None) is not None:
                self._flush()


class RedisStorage:
    """Upstash Redis over its HTTP REST API.

    The REST API is used rather than the Redis wire protocol because
    serverless functions can't hold a TCP connection pool open between
    invocations.
    """

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._token = token

    async def _command(self, *args: Any) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as http:
            response = await http.post(
                self._url,
                headers={"Authorization": f"Bearer {self._token}"},
                json=[str(a) for a in args],
            )
        response.raise_for_status()
        return response.json().get("result")

    async def get(self, key: str) -> Any | None:
        raw = await self._command("GET", key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        payload = json.dumps(value)
        if ttl_seconds:
            # EX makes Redis expire the key itself — PKCE state and chat
            # history clean themselves up without a sweeper.
            await self._command("SET", key, payload, "EX", ttl_seconds)
        else:
            await self._command("SET", key, payload)

    async def delete(self, key: str) -> None:
        await self._command("DEL", key)


def build_storage(settings: Any) -> Storage:
    """Redis when configured, otherwise a local file."""
    if settings.redis_url and settings.redis_token:
        return RedisStorage(settings.redis_url, settings.redis_token)
    return FileStorage(settings.state_dir)
