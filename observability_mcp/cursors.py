from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .errors import ToolError


@dataclass(frozen=True)
class LogCursorState:
    pit_id: str
    index_expression: str
    search_after: list[Any]
    search_args: dict[str, Any]
    profile_name: str
    limit: int
    seen: int


@dataclass(frozen=True)
class _StoredCursor:
    state: LogCursorState
    expires_at: float


class MemoryCursorStore:
    """One-time, expiring cursor tokens for a single MCP process."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._items: dict[str, _StoredCursor] = {}
        self._lock = asyncio.Lock()

    async def save(self, state: LogCursorState, token: str | None = None) -> str:
        token = token or f"lc_{secrets.token_urlsafe(18)}"
        async with self._lock:
            self._remove_expired_locked()
            self._items[token] = _StoredCursor(state, time.monotonic() + self._ttl_seconds)
        return token

    async def take(self, token: str) -> LogCursorState:
        if not token or len(token) > 128:
            raise ToolError("Invalid cursor")
        async with self._lock:
            self._remove_expired_locked()
            stored = self._items.pop(token, None)
        if stored is None:
            raise ToolError("Cursor does not exist or has expired; start a new search")
        return stored.state

    def _remove_expired_locked(self) -> None:
        now = time.monotonic()
        for token in [key for key, value in self._items.items() if value.expires_at <= now]:
            self._items.pop(token, None)
