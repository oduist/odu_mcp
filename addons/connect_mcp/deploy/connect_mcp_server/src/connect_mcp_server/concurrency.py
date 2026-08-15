from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from .errors import OdooApiError


@dataclass(slots=True)
class _Entry:
    lock: asyncio.Lock
    references: int = 0


class UserOperationLimiter:
    """Serialize Odoo operations per authenticated user without global blocking."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self._entries: dict[str, _Entry] = {}
        self._registry_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, subject: str) -> AsyncIterator[None]:
        async with self._registry_lock:
            entry = self._entries.get(subject)
            if entry is None:
                entry = _Entry(lock=asyncio.Lock())
                self._entries[subject] = entry
            entry.references += 1

        acquired = False
        try:
            try:
                await asyncio.wait_for(entry.lock.acquire(), timeout=self.timeout_seconds)
                acquired = True
            except TimeoutError as exc:
                raise OdooApiError(
                    code="user_operation_busy",
                    message="Another MCP operation for this Odoo user is still running.",
                    retryable=True,
                    status_code=429,
                ) from exc
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._registry_lock:
                entry.references -= 1
                if entry.references == 0 and not entry.lock.locked():
                    self._entries.pop(subject, None)
