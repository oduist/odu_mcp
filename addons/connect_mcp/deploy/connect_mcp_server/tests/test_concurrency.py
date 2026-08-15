from __future__ import annotations

import asyncio

import pytest

from connect_mcp_server.concurrency import UserOperationLimiter
from connect_mcp_server.errors import OdooApiError


@pytest.mark.asyncio
async def test_limiter_serializes_same_user_without_blocking_another_user() -> None:
    limiter = UserOperationLimiter(timeout_seconds=0.05)
    alice_entered = asyncio.Event()
    release_alice = asyncio.Event()

    async def hold_alice() -> None:
        async with limiter.acquire("alice"):
            alice_entered.set()
            await release_alice.wait()

    task = asyncio.create_task(hold_alice())
    await alice_entered.wait()
    try:
        async with limiter.acquire("bob"):
            pass
        with pytest.raises(OdooApiError, match="user_operation_busy"):
            async with limiter.acquire("alice"):
                pass
    finally:
        release_alice.set()
        await task

    assert limiter._entries == {}
