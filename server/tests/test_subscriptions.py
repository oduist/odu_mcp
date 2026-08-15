from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from mcp.shared.subscriptions import ResourceUpdated

from odoo_agent_mcp.config import Settings
from odoo_agent_mcp.subscriptions import EventTicket, OdooEventBridge, SubscriptionPublisher


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "odoo_url": "https://odoo.example.test",
        "events_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_event_bridge_uses_explicit_websocket_endpoint_and_matching_origin() -> None:
    bridge = OdooEventBridge(
        _settings(events_url="ws://odoo-evented:8072/odoo_mcp/v1/events"),
        SubscriptionPublisher(),
    )

    try:
        assert bridge._websocket_url() == "ws://odoo-evented:8072/odoo_mcp/v1/events"
        assert bridge._origin() == "http://odoo-evented:8072"
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
async def test_fastmcp_subscription_adapter_registers_and_publishes() -> None:
    publisher = SubscriptionPublisher()
    server = FastMCP("test")
    publisher.install(server)
    events: list[ResourceUpdated] = []
    unsubscribe = publisher.subscribe("odoo:prod:7", events.append)
    try:
        await publisher.publish("odoo:prod:7", "odoo://approval/abc")
        await publisher.publish("odoo:prod:8", "odoo://approval/abc")
    finally:
        unsubscribe()
        publisher.close()

    assert "subscriptions/listen" in server._mcp_server._request_handlers
    assert events == [ResourceUpdated(uri="odoo://approval/abc")]
    assert publisher._handlers == {}
    assert publisher._buses == {}


@pytest.mark.asyncio
async def test_event_bridge_mints_ticket_and_filters_deduplicated_events() -> None:
    seen_authorization = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_authorization
        seen_authorization = request.headers["Authorization"]
        return httpx.Response(
            201,
            json={"ok": True, "data": {"ticket": "event-ticket", "expires_in": 600}},
            request=request,
        )

    publisher = SubscriptionPublisher()
    published: list[tuple[str, str]] = []

    async def capture(subject: str, uri: str) -> None:
        published.append((subject, uri))

    publisher.publish = capture  # type: ignore[method-assign]
    bridge = OdooEventBridge(
        _settings(),
        publisher,
        transport=httpx.MockTransport(handler),
    )
    token = AccessToken(
        token="odoo-key",
        client_id="odoo:prod:7",
        subject="odoo:prod:7",
        scopes=["mcp"],
    )
    try:
        ticket = await bridge._mint_ticket(token.token)
        assert ticket.token == "event-ticket"
        assert ticket.expires_at > time.monotonic()
        message = json.dumps(
            [
                {
                    "id": 1,
                    "message": {
                        "type": "odoo_mcp_resource_updated",
                        "payload": {
                            "type": "resource.updated",
                            "uri": "odoo://approval/abc",
                            "version": 3,
                        },
                    },
                }
            ]
        )
        await bridge._handle_message(token.subject or "", message)
        await bridge._handle_message(token.subject or "", message)
    finally:
        await bridge.aclose()

    assert seen_authorization == "Bearer odoo-key"
    assert published == [("odoo:prod:7", "odoo://approval/abc")]


@pytest.mark.asyncio
async def test_event_watcher_mints_one_ticket_clears_key_and_stops_at_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = OdooEventBridge(_settings(), SubscriptionPublisher())
    minted_with: list[str] = []
    websocket_ready = asyncio.Event()

    async def mint_ticket(bearer_token: str):
        minted_with.append(bearer_token)
        return EventTicket(token="event-ticket", expires_at=time.monotonic() + 0.2)

    class FakeWebSocket:
        async def send(self, _message: str) -> None:
            websocket_ready.set()

        async def recv(self) -> str:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    @asynccontextmanager
    async def fake_connect(*_args, **_kwargs):
        yield FakeWebSocket()

    monkeypatch.setattr("odoo_agent_mcp.subscriptions.connect", fake_connect)
    bridge._mint_ticket = mint_ticket  # type: ignore[method-assign]
    task = asyncio.create_task(bridge._watch("odoo:prod:7", "odoo-key"))
    try:
        await asyncio.wait_for(websocket_ready.wait(), timeout=1)
        frame = task.get_coro().cr_frame
        assert frame is not None
        assert "bearer_token" not in frame.f_locals
        await asyncio.wait_for(task, timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.aclose()

    assert minted_with == ["odoo-key"]


@pytest.mark.asyncio
async def test_event_watcher_lifetime_follows_active_subscriptions() -> None:
    publisher = SubscriptionPublisher()
    bridge = OdooEventBridge(_settings(), publisher)
    token = AccessToken(
        token="odoo-key",
        client_id="odoo:prod:7",
        subject="odoo:prod:7",
        scopes=["mcp"],
    )
    cancelled = asyncio.Event()
    publisher._handler("odoo:prod:7")
    publisher._handler("odoo:prod:8")
    bridge._versions[("odoo:prod:7", "odoo://approval/abc")] = 3
    bridge._versions[("odoo:prod:8", "odoo://approval/xyz")] = 4

    async def worker() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def ensure_watcher(access_token: AccessToken) -> None:
        subject = access_token.subject or ""
        if subject not in bridge._watchers:
            bridge._watchers[subject] = asyncio.create_task(worker())
            await asyncio.sleep(0)

    bridge.ensure_watcher = ensure_watcher  # type: ignore[method-assign]
    try:
        async with bridge.watch_subscription(token):
            async with bridge.watch_subscription(token):
                assert bridge._references == {"odoo:prod:7": 2}
            assert bridge._references == {"odoo:prod:7": 1}
            assert not cancelled.is_set()
        assert bridge._references == {}
        assert cancelled.is_set()
        assert "odoo:prod:7" not in bridge._watchers
        assert all(key[0] != "odoo:prod:7" for key in bridge._versions)
        assert "odoo:prod:7" not in publisher._handlers
        assert "odoo:prod:7" not in publisher._buses
        assert bridge._versions == {("odoo:prod:8", "odoo://approval/xyz"): 4}
        assert "odoo:prod:8" in publisher._handlers
        assert "odoo:prod:8" in publisher._buses
    finally:
        await bridge.aclose()

    assert bridge._versions == {}
    assert publisher._handlers == {}
    assert publisher._buses == {}
