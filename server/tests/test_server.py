from __future__ import annotations

import httpx
import pytest

from odoo_agent_mcp.config import Settings
from odoo_agent_mcp.server import create_server

ALL_GROUPS = frozenset(
    {
        "core",
        "write",
        "collaboration",
        "documents",
        "sales",
        "accounting",
        "inventory",
        "projects",
        "hr",
    }
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "odoo_url": "https://odoo.example.test",
        "tool_groups": ALL_GROUPS,
        "events_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_registers_complete_but_bounded_contract() -> None:
    server = create_server(_settings())

    tools = {tool.name: tool for tool in await server.list_tools()}
    resources = await server.list_resources()
    templates = await server.list_resource_templates()
    prompts = await server.list_prompts()

    assert len(tools) == 24
    assert {
        "odoo_search_records",
        "odoo_preview_update",
        "odoo_execute_approved_change",
        "odoo_read_attachment",
        "odoo_sales_snapshot",
        "odoo_receivables_aging",
        "odoo_inventory_risk",
        "odoo_project_status",
        "odoo_absence_overview",
    } <= tools.keys()
    execute_schema = tools["odoo_execute_approved_change"].parameters
    assert "force" not in execute_schema["properties"]
    assert len(resources) + len(templates) == 4
    assert len(prompts) == 6
    assert "subscriptions/listen" in server._mcp_server._request_handlers


@pytest.mark.asyncio
async def test_http_health_and_readiness_routes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/odoo_mcp/v1/health"
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={"status": "ok", "service": "odoo_mcp_control"},
            request=request,
        )

    server = create_server(_settings(), client_transport=httpx.MockTransport(handler))
    app = server.http_app(path="/mcp", stateless_http=True, json_response=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
        assert (await client.get("/healthz")).json()["status"] == "ok"
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependency": "odoo",
        "odoo": "ok",
    }


@pytest.mark.asyncio
async def test_auth_challenge_does_not_advertise_missing_resource_metadata() -> None:
    server = create_server(_settings())
    app = server.http_app(path="/mcp", stateless_http=True, json_response=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
        response = await client.post("/mcp", json={})

    assert response.status_code == 401
    challenge = response.headers["WWW-Authenticate"]
    assert challenge == 'Bearer scope="mcp"'
    assert "resource_metadata" not in challenge


@pytest.mark.asyncio
async def test_readiness_returns_503_without_leaking_odoo_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "ok": False,
                "error": {
                    "code": "connector_unavailable",
                    "message": "internal detail",
                    "retryable": True,
                },
            },
            request=request,
        )

    server = create_server(
        _settings(retry_attempts=1),
        client_transport=httpx.MockTransport(handler),
    )
    app = server.http_app(path="/mcp", stateless_http=True, json_response=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "dependency": "odoo",
        "code": "connector_unavailable",
    }
    assert "internal detail" not in response.text
