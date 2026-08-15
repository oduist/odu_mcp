from __future__ import annotations

import asyncio

import pytest

from connect_mcp_server.concurrency import OperationQueue
from connect_mcp_server.errors import OdooApiError


def test_queue_rejects_unknown_scope() -> None:
    with pytest.raises(ValueError, match="scope"):
        OperationQueue(scope="tenant", timeout_seconds=1, max_queue_size=10)


@pytest.mark.asyncio
async def test_user_queue_serializes_same_user_without_blocking_another_user() -> None:
    queue = OperationQueue(scope="user", timeout_seconds=0.05, max_queue_size=10)
    alice_entered = asyncio.Event()
    release_alice = asyncio.Event()

    async def hold_alice() -> None:
        async with queue.acquire("alice"):
            alice_entered.set()
            await release_alice.wait()

    task = asyncio.create_task(hold_alice())
    await alice_entered.wait()
    try:
        async with queue.acquire("bob"):
            pass
        with pytest.raises(OdooApiError, match="operation_queue_timeout"):
            async with queue.acquire("alice"):
                pass
    finally:
        release_alice.set()
        await task

    assert queue._entries == {}


@pytest.mark.asyncio
async def test_global_queue_runs_different_users_in_fifo_order() -> None:
    queue = OperationQueue(scope="global", timeout_seconds=0, max_queue_size=10)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def run(name: str, subject: str, *, hold: bool = False) -> None:
        async with queue.acquire(subject):
            order.append(name)
            if hold:
                first_entered.set()
                await release_first.wait()

    first = asyncio.create_task(run("first", "alice", hold=True))
    await first_entered.wait()
    second = asyncio.create_task(run("second", "bob"))
    await asyncio.sleep(0)
    third = asyncio.create_task(run("third", "carol"))
    await asyncio.sleep(0)

    release_first.set()
    await asyncio.gather(first, second, third)

    assert order == ["first", "second", "third"]
    assert queue._entries == {}


@pytest.mark.asyncio
async def test_cancelled_waiter_is_removed_from_queue() -> None:
    queue = OperationQueue(scope="global", timeout_seconds=1, max_queue_size=10)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_first() -> None:
        async with queue.acquire("alice"):
            first_entered.set()
            await release_first.wait()

    async def wait_second() -> None:
        async with queue.acquire("bob"):
            pass

    first = asyncio.create_task(hold_first())
    await first_entered.wait()
    second = asyncio.create_task(wait_second())
    await asyncio.sleep(0)

    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release_first.set()
    await first

    assert queue._entries == {}


@pytest.mark.asyncio
async def test_global_queue_rejects_requests_above_maximum_size() -> None:
    queue = OperationQueue(scope="global", timeout_seconds=1, max_queue_size=1)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    release_second = asyncio.Event()

    async def hold_first() -> None:
        async with queue.acquire("alice"):
            first_entered.set()
            await release_first.wait()

    async def hold_second() -> None:
        async with queue.acquire("bob"):
            second_entered.set()
            await release_second.wait()

    first = asyncio.create_task(hold_first())
    await first_entered.wait()
    second = asyncio.create_task(hold_second())
    await asyncio.sleep(0)
    try:
        with pytest.raises(OdooApiError, match="operation_queue_full"):
            async with queue.acquire("carol"):
                pass
    finally:
        release_first.set()
        await second_entered.wait()
        release_second.set()
        await asyncio.gather(first, second)

    assert queue._entries == {}
