from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from mcp.shared.subscriptions import ResourceUpdated

from connect_mcp_server.config import Settings
from connect_mcp_server.subscriptions import EventTicket, OdooEventBridge, SubscriptionPublisher


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "odoo_url": "https://odoo.example.test",
        "events_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_event_bridge_uses_explicit_longpoll_endpoint() -> None:
    bridge = OdooEventBridge(
        _settings(events_url="http://odoo-evented:8072/connect_mcp/v1/events"),
        SubscriptionPublisher(),
    )

    try:
        assert bridge._events_url() == "http://odoo-evented:8072/connect_mcp/v1/events"
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
                        "type": "connect_mcp_resource_updated",
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
async def test_event_watcher_mints_one_ticket_clears_key_and_stops_at_expiry() -> None:
    minted_with: list[str] = []
    poll_ready = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer event-ticket"
        poll_ready.set()
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={"notifications": [], "last": 0},
            request=request,
        )

    bridge = OdooEventBridge(
        _settings(),
        SubscriptionPublisher(),
        transport=httpx.MockTransport(handler),
    )

    async def mint_ticket(bearer_token: str):
        minted_with.append(bearer_token)
        return EventTicket(token="event-ticket", expires_at=time.monotonic() + 0.2)

    bridge._mint_ticket = mint_ticket  # type: ignore[method-assign]
    task = asyncio.create_task(bridge._watch("odoo:prod:7", "odoo-key"))
    try:
        await asyncio.wait_for(poll_ready.wait(), timeout=1)
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "subject"),
    [
        (_settings(events_enabled=False), "odoo:prod:7"),
        (_settings(), None),
    ],
)
async def test_watch_subscription_skips_when_events_are_unavailable(
    settings: Settings,
    subject: str | None,
) -> None:
    bridge = OdooEventBridge(settings, SubscriptionPublisher())
    token = AccessToken(token="key", client_id="client", subject=subject, scopes=["mcp"])
    try:
        async with bridge.watch_subscription(token):
            assert bridge._references == {}
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
async def test_ensure_watcher_reuses_live_task_and_removes_completed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = OdooEventBridge(_settings(), SubscriptionPublisher())
    started = asyncio.Event()
    release = asyncio.Event()

    async def worker(_subject: str, _token: str) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(bridge, "_watch", worker)
    token = AccessToken(
        token="key",
        client_id="odoo:prod:7",
        subject="odoo:prod:7",
        scopes=["mcp"],
    )
    try:
        await bridge.ensure_watcher(token)
        await started.wait()
        task = bridge._watchers[token.subject or ""]
        await bridge.ensure_watcher(token)
        assert bridge._watchers[token.subject or ""] is task
        release.set()
        await task
        await asyncio.sleep(0)
        assert token.subject not in bridge._watchers
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 500, "http", "value"])
async def test_event_watcher_stops_when_ticket_minting_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure: int | str,
) -> None:
    bridge = OdooEventBridge(_settings(), SubscriptionPublisher())

    async def fail_mint(_bearer_token: str) -> EventTicket:
        if isinstance(failure, int):
            request = httpx.Request("POST", "https://odoo.example.test/ticket")
            response = httpx.Response(failure, request=request)
            raise httpx.HTTPStatusError("ticket failed", request=request, response=response)
        if failure == "http":
            raise httpx.ConnectError("offline")
        raise ValueError("invalid ticket")

    monkeypatch.setattr(bridge, "_mint_ticket", fail_mint)
    try:
        await bridge._watch("odoo:prod:7", "key")
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": {"ticket": "", "expires_in": 10}},
        {"data": {"ticket": "ticket", "expires_in": True}},
        {"data": {"ticket": "ticket", "expires_in": 0}},
    ],
)
async def test_event_ticket_rejects_invalid_odoo_envelopes(payload: object) -> None:
    bridge = OdooEventBridge(
        _settings(),
        SubscriptionPublisher(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json=payload, request=request)
        ),
    )
    try:
        with pytest.raises(ValueError, match="invalid MCP event ticket"):
            await bridge._mint_ticket("key")
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
async def test_event_message_parser_ignores_malformed_or_untrusted_notifications() -> None:
    publisher = SubscriptionPublisher()
    published: list[tuple[str, str]] = []

    async def capture(subject: str, uri: str) -> None:
        published.append((subject, uri))

    publisher.publish = capture  # type: ignore[method-assign]
    bridge = OdooEventBridge(_settings(), publisher)
    invalid_notifications = [
        None,
        {"message": "invalid"},
        {"message": {"type": "other"}},
        {"message": {"type": "connect_mcp_resource_updated", "payload": "invalid"}},
        {
            "message": {
                "type": "connect_mcp_resource_updated",
                "payload": {"type": "other", "uri": "odoo://approval/1", "version": 1},
            }
        },
        {
            "message": {
                "type": "connect_mcp_resource_updated",
                "payload": {"type": "resource.updated", "uri": "https://invalid", "version": 1},
            }
        },
        {
            "message": {
                "type": "connect_mcp_resource_updated",
                "payload": {
                    "type": "resource.updated",
                    "uri": "odoo://approval/1",
                    "version": True,
                },
            }
        },
    ]
    try:
        await bridge._handle_message("subject", b"\xff")
        await bridge._handle_message("subject", "not-json")
        await bridge._handle_message("subject", json.dumps({"message": "not-a-list"}))
        await bridge._handle_message("subject", json.dumps(invalid_notifications))
    finally:
        await bridge.aclose()

    assert published == []


@pytest.mark.asyncio
async def test_event_bridge_derives_longpoll_url() -> None:
    insecure = OdooEventBridge(
        _settings(odoo_url="https://odoo.example.test/base", verify_tls=False),
        SubscriptionPublisher(),
    )
    plain = OdooEventBridge(
        _settings(odoo_url="http://odoo.example.test"),
        SubscriptionPublisher(),
    )
    try:
        assert insecure._events_url() == "https://odoo.example.test/connect_mcp/v1/events"
        assert plain._events_url() == "http://odoo.example.test/connect_mcp/v1/events"
    finally:
        await insecure.aclose()
        await plain.aclose()
