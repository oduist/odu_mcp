from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from .errors import OdooApiError


@dataclass(slots=True)
class _Entry:
    active: bool = False
    waiters: deque[asyncio.Future[None]] = field(default_factory=deque)


class OperationQueue:
    """Run Odoo operations through a bounded FIFO queue."""

    def __init__(
        self,
        *,
        scope: str,
        timeout_seconds: float,
        max_queue_size: int,
    ) -> None:
        if scope not in {"global", "user"}:
            raise ValueError("scope must be 'global' or 'user'")
        self.scope = scope
        self.timeout_seconds = timeout_seconds
        self.max_queue_size = max_queue_size
        self._entries: dict[str, _Entry] = {}
        self._registry_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, subject: str) -> AsyncIterator[None]:
        key = "global" if self.scope == "global" else subject
        waiter: asyncio.Future[None] | None = None
        async with self._registry_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry()
                self._entries[key] = entry
            if entry.active:
                if len(entry.waiters) >= self.max_queue_size:
                    raise OdooApiError(
                        code="operation_queue_full",
                        message="The MCP operation queue is full. Retry later.",
                        retryable=True,
                        status_code=429,
                    )
                waiter = asyncio.get_running_loop().create_future()
                entry.waiters.append(waiter)
            else:
                entry.active = True

        acquired = False
        try:
            if waiter is not None:
                try:
                    if self.timeout_seconds:
                        await asyncio.wait_for(waiter, timeout=self.timeout_seconds)
                    else:
                        await waiter
                except TimeoutError as exc:
                    await self._discard_waiter(entry, waiter)
                    raise OdooApiError(
                        code="operation_queue_timeout",
                        message="The MCP operation did not reach the front of the queue in time.",
                        retryable=True,
                        status_code=429,
                    ) from exc
                except BaseException:
                    await self._discard_waiter(entry, waiter)
                    raise
            acquired = True
            yield
        finally:
            if acquired:
                await self._release(key, entry)

    async def _discard_waiter(
        self,
        entry: _Entry,
        waiter: asyncio.Future[None],
    ) -> None:
        async with self._registry_lock:
            entry.waiters.remove(waiter)

    async def _release(self, key: str, entry: _Entry) -> None:
        async with self._registry_lock:
            if entry.waiters:
                waiter = entry.waiters.popleft()
                waiter.set_result(None)
                return
            entry.active = False
            self._entries.pop(key, None)
