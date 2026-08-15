from __future__ import annotations

import json
import time

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from mcp.shared.subscriptions import ResourceUpdated

from odoo_agent_mcp.config import Settings
from odoo_agent_mcp.subscriptions import OdooEventBridge, SubscriptionPublisher


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "odoo_url": "https://odoo.example.test",
        "events_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


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
