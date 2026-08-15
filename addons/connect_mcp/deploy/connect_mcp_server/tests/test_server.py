from __future__ import annotations

import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

import connect_mcp_server.server as server_module
from connect_mcp_server.config import Settings
from connect_mcp_server.server import create_server

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


TOOL_CASES = [
    ("odoo_server_info", {}, "system.info", None),
    ("odoo_whoami", {}, "identity.whoami", None),
    ("odoo_list_models", {}, "models.list", None),
    ("odoo_describe_model", {"model": "res.partner"}, "models.describe", {"model": "res.partner"}),
    (
        "odoo_search_records",
        {
            "model": "res.partner",
            "domain": [["active", "=", True]],
            "fields": ["name"],
            "offset": 2,
            "limit": 5,
            "order": "name",
        },
        "records.search",
        {
            "model": "res.partner",
            "domain": [["active", "=", True]],
            "fields": ["name"],
            "offset": 2,
            "limit": 5,
            "order": "name",
        },
    ),
    (
        "odoo_get_record",
        {"model": "res.partner", "ids": [1, 2], "fields": ["name"]},
        "records.read",
        {"model": "res.partner", "ids": [1, 2], "fields": ["name"]},
    ),
    (
        "odoo_count_records",
        {"model": "res.partner", "domain": [["active", "=", True]]},
        "records.count",
        {"model": "res.partner", "domain": [["active", "=", True]]},
    ),
    (
        "odoo_aggregate_records",
        {
            "model": "sale.order",
            "fields": ["amount_total:sum"],
            "domain": [["state", "=", "sale"]],
            "groupby": ["currency_id"],
            "limit": 10,
        },
        "records.aggregate",
        {
            "model": "sale.order",
            "domain": [["state", "=", "sale"]],
            "fields": ["amount_total:sum"],
            "groupby": ["currency_id"],
            "limit": 10,
        },
    ),
    (
        "odoo_get_change_status",
        {"approval_id": "approval-1"},
        "changes.status",
        {"approval_id": "approval-1"},
    ),
    (
        "odoo_execute_approved_change",
        {"approval_id": "approval-1"},
        "changes.execute",
        {"approval_id": "approval-1"},
    ),
    (
        "odoo_preview_create",
        {"model": "res.partner", "values": {"name": "Test"}, "idempotency_key": "create-01"},
        "changes.preview",
        {
            "action": "record.create",
            "payload": {"model": "res.partner", "values": {"name": "Test"}},
            "idempotency_key": "create-01",
        },
    ),
    (
        "odoo_preview_update",
        {
            "model": "res.partner",
            "ids": [1],
            "values": {"name": "Updated"},
            "idempotency_key": "update-01",
        },
        "changes.preview",
        {
            "action": "record.update",
            "payload": {"model": "res.partner", "ids": [1], "values": {"name": "Updated"}},
            "idempotency_key": "update-01",
        },
    ),
    (
        "odoo_preview_delete",
        {"model": "res.partner", "ids": [1], "idempotency_key": "delete-01"},
        "changes.preview",
        {
            "action": "record.delete",
            "payload": {"model": "res.partner", "ids": [1]},
            "idempotency_key": "delete-01",
        },
    ),
    (
        "odoo_preview_method",
        {
            "model": "sale.order",
            "method": "action_confirm",
            "ids": [1],
            "args": ["value"],
            "kwargs": {"flag": True},
            "idempotency_key": "method-01",
        },
        "changes.preview",
        {
            "action": "method.call",
            "payload": {
                "model": "sale.order",
                "ids": [1],
                "method": "action_confirm",
                "args": ["value"],
                "kwargs": {"flag": True},
            },
            "idempotency_key": "method-01",
        },
    ),
    (
        "odoo_preview_post_message",
        {"model": "res.partner", "id": 1, "body": "Hello", "idempotency_key": "message-01"},
        "changes.preview",
        {
            "action": "message.post",
            "payload": {"model": "res.partner", "id": 1, "body": "Hello"},
            "idempotency_key": "message-01",
        },
    ),
    (
        "odoo_preview_schedule_activity",
        {
            "model": "res.partner",
            "id": 1,
            "activity_type": "mail.mail_activity_data_todo",
            "summary": "Call",
            "note": "Soon",
            "date_deadline": "2026-08-20",
            "user_id": 7,
            "idempotency_key": "activity-01",
        },
        "changes.preview",
        {
            "action": "activity.schedule",
            "payload": {
                "model": "res.partner",
                "id": 1,
                "activity_type": "mail.mail_activity_data_todo",
                "summary": "Call",
                "note": "Soon",
                "date_deadline": "2026-08-20",
                "user_id": 7,
            },
            "idempotency_key": "activity-01",
        },
    ),
    ("odoo_read_attachment", {"attachment_id": 5}, "attachments.read", {"attachment_id": 5}),
    (
        "odoo_preview_upload_attachment",
        {
            "model": "res.partner",
            "id": 1,
            "name": "note.txt",
            "content_base64": "YQ==",
            "mimetype": "text/plain",
            "idempotency_key": "upload-01",
        },
        "changes.preview",
        {
            "action": "attachment.create",
            "payload": {
                "model": "res.partner",
                "id": 1,
                "name": "note.txt",
                "content_base64": "YQ==",
                "mimetype": "text/plain",
            },
            "idempotency_key": "upload-01",
        },
    ),
    (
        "odoo_render_report",
        {"report": "sale.action_report_saleorder", "ids": [1]},
        "reports.render",
        {"report": "sale.action_report_saleorder", "ids": [1]},
    ),
    (
        "odoo_sales_snapshot",
        {"date_from": "2026-01-01", "date_to": "2026-01-31", "limit": 8},
        "records.aggregate",
        {
            "model": "sale.order",
            "domain": [["date_order", ">=", "2026-01-01"], ["date_order", "<=", "2026-01-31"]],
            "fields": ["amount_total:sum"],
            "groupby": ["state", "currency_id"],
            "limit": 8,
        },
    ),
    (
        "odoo_receivables_aging",
        {"date_to": "2026-01-31", "limit": 9},
        "records.aggregate",
        {
            "model": "account.move.line",
            "domain": [
                ["move_id.state", "=", "posted"],
                ["account_id.account_type", "=", "asset_receivable"],
                ["amount_residual", "!=", 0],
                ["date_maturity", "<=", "2026-01-31"],
            ],
            "fields": ["amount_residual:sum"],
            "groupby": ["partner_id", "company_currency_id"],
            "limit": 9,
        },
    ),
    (
        "odoo_inventory_risk",
        {"limit": 7},
        "records.search",
        {
            "model": "product.product",
            "domain": [["active", "=", True], ["is_storable", "=", True]],
            "fields": [
                "display_name",
                "default_code",
                "qty_available",
                "free_qty",
                "virtual_available",
            ],
            "offset": 0,
            "limit": 7,
            "order": "virtual_available asc",
        },
    ),
    (
        "odoo_project_status",
        {"limit": 6},
        "records.aggregate",
        {
            "model": "project.task",
            "domain": [["active", "=", True]],
            "fields": ["id:count"],
            "groupby": ["project_id", "stage_id"],
            "limit": 6,
        },
    ),
    (
        "odoo_absence_overview",
        {"date_from": "2026-02-01", "date_to": "2026-02-28", "limit": 5},
        "records.search",
        {
            "model": "hr.leave",
            "domain": [
                ["state", "in", ["confirm", "validate", "validate1"]],
                ["date_to", ">=", "2026-02-01"],
                ["date_from", "<=", "2026-02-28"],
            ],
            "fields": [
                "employee_id",
                "holiday_status_id",
                "state",
                "date_from",
                "date_to",
                "number_of_days",
            ],
            "limit": 5,
            "order": "date_from asc",
        },
    ),
]


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
@pytest.mark.parametrize(("tool_name", "arguments", "operation", "params"), TOOL_CASES)
async def test_tools_forward_validated_odoo_contract(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, object],
    operation: str,
    params: dict[str, object] | None,
) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    async def fake_execute(_ctx: object, called_operation: str, called_params=None):
        calls.append((called_operation, called_params))
        return {}

    monkeypatch.setattr(server_module, "_execute", fake_execute)
    server = create_server(_settings())

    result = await server.call_tool(tool_name, arguments)

    assert result.is_error is False
    assert calls == [(operation, params)]


@pytest.mark.asyncio
async def test_resources_forward_validated_odoo_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    async def fake_execute(_ctx: object, operation: str, params=None):
        calls.append((operation, params))
        return {"operation": operation}

    monkeypatch.setattr(server_module, "_execute", fake_execute)
    server = create_server(_settings())
    cases = [
        ("odoo://server/capabilities", "capabilities", None),
        ("odoo://model/res.partner/schema", "models.describe", {"model": "res.partner"}),
        (
            "odoo://record/res.partner/3",
            "records.read",
            {"model": "res.partner", "ids": [3]},
        ),
        ("odoo://approval/approval-1", "changes.status", {"approval_id": "approval-1"}),
    ]

    for uri, operation, _params in cases:
        result = await server.read_resource(uri)
        assert json.loads(result.contents[0].content) == {"operation": operation}

    assert calls == [(operation, params) for _, operation, params in cases]


@pytest.mark.asyncio
async def test_prompts_render_operational_safety_guidance() -> None:
    server = create_server(_settings())
    cases = [
        ("analyze_records", {"model": "res.partner", "objective": "Review"}, "explicit field list"),
        ("summarize_record", {"model": "res.partner", "record_id": "3"}, "#3"),
        ("prepare_change_plan", {"objective": "Rename a partner"}, "preview is not execution"),
        (
            "investigate_access_denial",
            {"operation": "records.read", "model": "res.partner"},
            "Do not attempt",
        ),
        ("sales_review", {"date_from": "2026-01-01", "date_to": "2026-01-31"}, "currencies"),
        ("receivables_review", {"as_of": "2026-01-31"}, "do not post"),
    ]

    for prompt_name, arguments, expected_text in cases:
        result = await server.render_prompt(prompt_name, arguments)
        assert expected_text in result.messages[0].content.text


@pytest.mark.asyncio
async def test_tool_validation_rejects_invalid_odoo_identifiers() -> None:
    server = create_server(_settings())

    with pytest.raises(ToolError, match="invalid_arguments"):
        await server.call_tool("odoo_describe_model", {"model": "res partner"})


@pytest.mark.asyncio
async def test_http_health_and_readiness_routes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/connect_mcp/v1/health"
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={"status": "ok", "service": "connect_mcp"},
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
