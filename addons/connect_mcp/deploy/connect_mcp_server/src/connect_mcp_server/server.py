from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from mcp_types import ToolAnnotations
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import OdooApiKeyVerifier
from .client import OdooControlClient
from .concurrency import UserOperationLimiter
from .config import Settings
from .errors import OdooApiError
from .schemas import (
    AggregateRequest,
    ApprovalRequest,
    AttachmentReadRequest,
    DomainSummaryRequest,
    ModelRequest,
    PreviewActivity,
    PreviewAttachment,
    PreviewCreate,
    PreviewDelete,
    PreviewMessage,
    PreviewMethod,
    PreviewUpdate,
    ReadRequest,
    ReportRequest,
    SearchRequest,
)
from .subscriptions import OdooEventBridge, SubscriptionPublisher

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
PREVIEW = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
EXECUTE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    client: OdooControlClient
    limiter: UserOperationLimiter


def _runtime(ctx: Context) -> Runtime:
    runtime = ctx.request_context.lifespan_context
    if not isinstance(runtime, Runtime):  # pragma: no cover - framework guard
        raise ToolError("MCP runtime is not available.")
    return runtime


async def _execute(
    ctx: Context,
    operation: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        raise ToolError("authentication_required: A valid Connect MCP API key is required.")
    try:
        runtime = _runtime(ctx)
        async with runtime.limiter.acquire(access_token.subject):
            return await runtime.client.execute(
                operation,
                params,
                bearer_token=access_token.token,
                request_id=str(uuid.uuid4()),
            )
    except OdooApiError as exc:
        raise ToolError(str(exc)) from exc


def _validated(model_type: type, values: dict[str, Any]) -> dict[str, Any]:
    try:
        return model_type(**values).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ToolError(f"invalid_arguments: {exc}") from exc


def create_server(
    settings: Settings,
    *,
    client_transport: Any | None = None,
    auth_transport: Any | None = None,
) -> FastMCP:
    publisher = SubscriptionPublisher()
    event_bridge = OdooEventBridge(settings, publisher, transport=auth_transport)
    verifier = OdooApiKeyVerifier(
        settings,
        transport=auth_transport,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        client = OdooControlClient(settings, transport=client_transport)
        runtime = Runtime(
            settings=settings,
            client=client,
            limiter=UserOperationLimiter(settings.user_lock_timeout_seconds),
        )
        try:
            yield runtime
        finally:
            await client.aclose()
            await event_bridge.aclose()
            await verifier.aclose()

    mcp = FastMCP(
        name="Connect MCP Server",
        instructions=(
            "Use read tools freely within the Odoo policy. "
            "Do not invoke tools in parallel for the same Odoo identity. "
            "A preview is not an executed change. Mutations require an Odoo approval "
            "followed by odoo_execute_approved_change."
        ),
        lifespan=lifespan,
        auth=verifier,
    )
    publisher.install(mcp, watch_subscription=event_bridge.watch_subscription)
    _register_core(mcp)
    _register_resources(mcp)
    _register_prompts(mcp)
    if "write" in settings.tool_groups:
        _register_write(mcp)
    if "collaboration" in settings.tool_groups:
        _register_collaboration(mcp)
    if "documents" in settings.tool_groups:
        _register_documents(mcp)
    _register_domain_tools(mcp, settings.tool_groups)
    _register_health_routes(mcp, settings, client_transport=client_transport)
    return mcp


def _register_health_routes(
    mcp: FastMCP,
    settings: Settings,
    *,
    client_transport: Any | None = None,
) -> None:
    @mcp.custom_route(
        "/healthz",
        methods=["GET"],
        name="healthz",
        include_in_schema=False,
    )
    async def healthz(_request: Request) -> JSONResponse:
        """Process liveness. It deliberately does not contact Odoo."""
        return JSONResponse({"status": "ok", "service": "connect-mcp-server"})

    @mcp.custom_route(
        "/readyz",
        methods=["GET"],
        name="readyz",
        include_in_schema=False,
    )
    async def readyz(_request: Request) -> JSONResponse:
        """Readiness includes connector authentication and Odoo availability."""
        client = OdooControlClient(settings, transport=client_transport)
        try:
            result = await client.health()
        except OdooApiError as exc:
            return JSONResponse(
                {
                    "status": "unavailable",
                    "dependency": "odoo",
                    "code": exc.code,
                },
                status_code=503,
            )
        finally:
            await client.aclose()
        return JSONResponse(
            {
                "status": "ready",
                "dependency": "odoo",
                "odoo": result.get("status", "ok"),
            }
        )


def _register_core(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def odoo_server_info(ctx: Context) -> dict[str, Any]:
        """Return Odoo version, connector profile, companies, and enabled capabilities."""
        return await _execute(ctx, "system.info")

    @mcp.tool(annotations=READ_ONLY)
    async def odoo_whoami(ctx: Context) -> dict[str, Any]:
        """Return the effective Odoo user, company, language, timezone, and profile."""
        return await _execute(ctx, "identity.whoami")

    @mcp.tool(annotations=READ_ONLY)
    async def odoo_list_models(ctx: Context) -> dict[str, Any]:
        """List only Odoo models and operations allowed by the connector profile."""
        return await _execute(ctx, "models.list")

    @mcp.tool(annotations=READ_ONLY)
    async def odoo_describe_model(model: str, ctx: Context) -> dict[str, Any]:
        """Describe policy-visible fields and allowed operations for one model."""
        params = _validated(ModelRequest, {"model": model})
        return await _execute(ctx, "models.describe", params)

    @mcp.tool(annotations=READ_ONLY)
    async def odoo_search_records(
        model: str,
        ctx: Context,
        domain: list[Any] | None = None,
        fields: list[str] | None = None,
        offset: int = 0,
        limit: int = 100,
        order: str | None = None,
    ) -> dict[str, Any]:
        """Search and read records. Odoo always ANDs the profile forced domain."""
        params = _validated(
            SearchRequest,
            {
                "model": model,
                "domain": domain or [],
                "fields": fields,
                "offset": offset,
                "limit": limit,
                "order": order,
            },
        )
        return await _execute(ctx, "records.search", params)

    @mcp.tool(annotations=READ_ONLY)
    async def odoo_get_record(
        model: str,
        ids: list[int],
        ctx: Context,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read exact record IDs inside the configured Odoo policy."""
        params = _validated(
            ReadRequest,
            {"model": model, "ids": ids, "fields": fields},
        )
        return await _execute(ctx, "records.read", params)

    @mcp.tool(annotations=READ_ONLY)
    async def odoo_count_records(
        model: str,
        ctx: Context,
        domain: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Count records inside the policy and Odoo record rules."""
        params = _validated(ModelRequest, {"model": model})
        params["domain"] = domain or []
        return await _execute(ctx, "records.count", params)

    @mcp.tool(annotations=READ_ONLY)
    async def odoo_aggregate_records(
        model: str,
        fields: list[str],
        ctx: Context,
        domain: list[Any] | None = None,
        groupby: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Aggregate stored readable fields using safe Odoo read_group."""
        params = _validated(
            AggregateRequest,
            {
                "model": model,
                "domain": domain or [],
                "fields": fields,
                "groupby": groupby or [],
                "limit": limit,
            },
        )
        return await _execute(ctx, "records.aggregate", params)

    @mcp.tool(annotations=READ_ONLY)
    async def odoo_get_change_status(
        approval_id: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Return pending/approved/rejected/executed state for an exact change plan."""
        params = _validated(ApprovalRequest, {"approval_id": approval_id})
        return await _execute(ctx, "changes.status", params)

    @mcp.tool(annotations=EXECUTE)
    async def odoo_execute_approved_change(
        approval_id: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Execute an Odoo-approved immutable plan exactly once. There is no force mode."""
        params = _validated(ApprovalRequest, {"approval_id": approval_id})
        return await _execute(ctx, "changes.execute", params)


def _register_write(mcp: FastMCP) -> None:
    @mcp.tool(annotations=PREVIEW)
    async def odoo_preview_create(
        model: str,
        values: dict[str, Any] | list[dict[str, Any]],
        idempotency_key: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Validate and create an approval plan for record creation; does not write."""
        values_dict = _validated(
            PreviewCreate,
            {"model": model, "values": values, "idempotency_key": idempotency_key},
        )
        return await _execute(
            ctx,
            "changes.preview",
            {
                "action": "record.create",
                "payload": {"model": values_dict["model"], "values": values_dict["values"]},
                "idempotency_key": values_dict["idempotency_key"],
            },
        )

    @mcp.tool(annotations=PREVIEW)
    async def odoo_preview_update(
        model: str,
        ids: list[int],
        values: dict[str, Any],
        idempotency_key: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Create an approval plan containing the exact records and redacted update diff."""
        values_dict = _validated(
            PreviewUpdate,
            {
                "model": model,
                "ids": ids,
                "values": values,
                "idempotency_key": idempotency_key,
            },
        )
        return await _execute(
            ctx,
            "changes.preview",
            {
                "action": "record.update",
                "payload": {
                    "model": values_dict["model"],
                    "ids": values_dict["ids"],
                    "values": values_dict["values"],
                },
                "idempotency_key": values_dict["idempotency_key"],
            },
        )

    @mcp.tool(annotations=PREVIEW)
    async def odoo_preview_delete(
        model: str,
        ids: list[int],
        idempotency_key: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Create a high-risk approval plan for deletion; does not delete."""
        values_dict = _validated(
            PreviewDelete,
            {"model": model, "ids": ids, "idempotency_key": idempotency_key},
        )
        return await _execute(
            ctx,
            "changes.preview",
            {
                "action": "record.delete",
                "payload": {"model": values_dict["model"], "ids": values_dict["ids"]},
                "idempotency_key": values_dict["idempotency_key"],
            },
        )

    @mcp.tool(annotations=PREVIEW)
    async def odoo_preview_method(
        model: str,
        method: str,
        idempotency_key: str,
        ctx: Context,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a plan for an explicitly allowlisted public Odoo method."""
        values_dict = _validated(
            PreviewMethod,
            {
                "model": model,
                "method": method,
                "idempotency_key": idempotency_key,
                "ids": ids or [],
                "args": args or [],
                "kwargs": kwargs or {},
            },
        )
        payload = dict(values_dict)
        payload.pop("idempotency_key")
        return await _execute(
            ctx,
            "changes.preview",
            {
                "action": "method.call",
                "payload": payload,
                "idempotency_key": values_dict["idempotency_key"],
            },
        )


def _register_collaboration(mcp: FastMCP) -> None:
    @mcp.tool(annotations=PREVIEW)
    async def odoo_preview_post_message(
        model: str,
        id: int,
        body: str,
        idempotency_key: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Create a low-risk approval plan for a plain-text chatter message."""
        values = _validated(
            PreviewMessage,
            {
                "model": model,
                "id": id,
                "body": body,
                "idempotency_key": idempotency_key,
            },
        )
        key = values.pop("idempotency_key")
        return await _execute(
            ctx,
            "changes.preview",
            {"action": "message.post", "payload": values, "idempotency_key": key},
        )

    @mcp.tool(annotations=PREVIEW)
    async def odoo_preview_schedule_activity(
        model: str,
        id: int,
        idempotency_key: str,
        ctx: Context,
        activity_type: str = "mail.mail_activity_data_todo",
        summary: str = "",
        note: str = "",
        date_deadline: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a low-risk plan for an Odoo follow-up activity."""
        values = _validated(
            PreviewActivity,
            {
                "model": model,
                "id": id,
                "idempotency_key": idempotency_key,
                "activity_type": activity_type,
                "summary": summary,
                "note": note,
                "date_deadline": date_deadline,
                "user_id": user_id,
            },
        )
        key = values.pop("idempotency_key")
        return await _execute(
            ctx,
            "changes.preview",
            {"action": "activity.schedule", "payload": values, "idempotency_key": key},
        )


def _register_documents(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def odoo_read_attachment(
        attachment_id: int,
        ctx: Context,
    ) -> dict[str, Any]:
        """Read one policy-visible Odoo attachment subject to binary size limits."""
        params = _validated(AttachmentReadRequest, {"attachment_id": attachment_id})
        return await _execute(ctx, "attachments.read", params)

    @mcp.tool(annotations=PREVIEW)
    async def odoo_preview_upload_attachment(
        model: str,
        id: int,
        name: str,
        content_base64: str,
        idempotency_key: str,
        ctx: Context,
        mimetype: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Create an approval plan for a bounded base64 attachment upload."""
        values = _validated(
            PreviewAttachment,
            {
                "model": model,
                "id": id,
                "name": name,
                "content_base64": content_base64,
                "idempotency_key": idempotency_key,
                "mimetype": mimetype,
            },
        )
        key = values.pop("idempotency_key")
        return await _execute(
            ctx,
            "changes.preview",
            {"action": "attachment.create", "payload": values, "idempotency_key": key},
        )

    @mcp.tool(annotations=READ_ONLY)
    async def odoo_render_report(
        report: str,
        ids: list[int],
        ctx: Context,
    ) -> dict[str, Any]:
        """Render a policy-visible Odoo QWeb report as bounded base64 content."""
        params = _validated(ReportRequest, {"report": report, "ids": ids})
        return await _execute(ctx, "reports.render", params)


def _register_domain_tools(mcp: FastMCP, groups: frozenset[str]) -> None:
    if "sales" in groups:

        @mcp.tool(annotations=READ_ONLY)
        async def odoo_sales_snapshot(
            ctx: Context,
            date_from: str | None = None,
            date_to: str | None = None,
            limit: int = 20,
        ) -> dict[str, Any]:
            """Summarize sales orders by state and currency using policy-safe aggregation."""
            request = _validated(
                DomainSummaryRequest,
                {"date_from": date_from, "date_to": date_to, "limit": limit},
            )
            domain: list[Any] = []
            if request.get("date_from"):
                domain.append(["date_order", ">=", request["date_from"]])
            if request.get("date_to"):
                domain.append(["date_order", "<=", request["date_to"]])
            result = await _execute(
                ctx,
                "records.aggregate",
                {
                    "model": "sale.order",
                    "domain": domain,
                    "fields": ["amount_total:sum"],
                    "groupby": ["state", "currency_id"],
                    "limit": request["limit"],
                },
            )
            result["date_range"] = {
                "from": request.get("date_from"),
                "to": request.get("date_to"),
            }
            return result

    if "accounting" in groups:

        @mcp.tool(annotations=READ_ONLY)
        async def odoo_receivables_aging(
            ctx: Context,
            date_to: str | None = None,
            limit: int = 50,
        ) -> dict[str, Any]:
            """Summarize posted open receivables by partner and company currency."""
            request = _validated(
                DomainSummaryRequest,
                {"date_to": date_to, "limit": limit},
            )
            domain: list[Any] = [
                ["move_id.state", "=", "posted"],
                ["account_id.account_type", "=", "asset_receivable"],
                ["amount_residual", "!=", 0],
            ]
            if request.get("date_to"):
                domain.append(["date_maturity", "<=", request["date_to"]])
            result = await _execute(
                ctx,
                "records.aggregate",
                {
                    "model": "account.move.line",
                    "domain": domain,
                    "fields": ["amount_residual:sum"],
                    "groupby": ["partner_id", "company_currency_id"],
                    "limit": request["limit"],
                },
            )
            result["as_of"] = request.get("date_to")
            result["currency_warning"] = (
                "Groups may contain multiple currencies; do not add unlike currencies."
            )
            return result

    if "inventory" in groups:

        @mcp.tool(annotations=READ_ONLY)
        async def odoo_inventory_risk(
            ctx: Context,
            limit: int = 50,
        ) -> dict[str, Any]:
            """Return active stockable/consumable products with policy-visible stock signals."""
            params = _validated(
                SearchRequest,
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
                    "limit": limit,
                    "order": "virtual_available asc",
                },
            )
            return await _execute(ctx, "records.search", params)

    if "projects" in groups:

        @mcp.tool(annotations=READ_ONLY)
        async def odoo_project_status(
            ctx: Context,
            limit: int = 100,
        ) -> dict[str, Any]:
            """Summarize active project tasks by project and stage."""
            return await _execute(
                ctx,
                "records.aggregate",
                {
                    "model": "project.task",
                    "domain": [["active", "=", True]],
                    "fields": ["id:count"],
                    "groupby": ["project_id", "stage_id"],
                    "limit": limit,
                },
            )

    if "hr" in groups:

        @mcp.tool(annotations=READ_ONLY)
        async def odoo_absence_overview(
            ctx: Context,
            date_from: str | None = None,
            date_to: str | None = None,
            limit: int = 100,
        ) -> dict[str, Any]:
            """Read policy-visible approved/confirmed leave records for a date range."""
            request = _validated(
                DomainSummaryRequest,
                {"date_from": date_from, "date_to": date_to, "limit": limit},
            )
            domain: list[Any] = [["state", "in", ["confirm", "validate", "validate1"]]]
            if request.get("date_from"):
                domain.append(["date_to", ">=", request["date_from"]])
            if request.get("date_to"):
                domain.append(["date_from", "<=", request["date_to"]])
            return await _execute(
                ctx,
                "records.search",
                {
                    "model": "hr.leave",
                    "domain": domain,
                    "fields": [
                        "employee_id",
                        "holiday_status_id",
                        "state",
                        "date_from",
                        "date_to",
                        "number_of_days",
                    ],
                    "limit": request["limit"],
                    "order": "date_from asc",
                },
            )


def _register_resources(mcp: FastMCP) -> None:
    @mcp.resource(
        "odoo://server/capabilities",
        name="Odoo connector capabilities",
        mime_type="application/json",
    )
    async def capabilities_resource(ctx: Context) -> str:
        return json.dumps(await _execute(ctx, "capabilities"), ensure_ascii=False)

    @mcp.resource(
        "odoo://model/{model}/schema",
        name="Odoo model schema",
        mime_type="application/json",
    )
    async def model_schema_resource(model: str, ctx: Context) -> str:
        params = _validated(ModelRequest, {"model": model})
        return json.dumps(
            await _execute(ctx, "models.describe", params),
            ensure_ascii=False,
        )

    @mcp.resource(
        "odoo://record/{model}/{record_id}",
        name="Odoo record",
        mime_type="application/json",
    )
    async def record_resource(model: str, record_id: int, ctx: Context) -> str:
        params = _validated(
            ReadRequest,
            {"model": model, "ids": [record_id], "fields": None},
        )
        return json.dumps(
            await _execute(ctx, "records.read", params),
            ensure_ascii=False,
        )

    @mcp.resource(
        "odoo://approval/{approval_id}",
        name="Odoo change approval",
        mime_type="application/json",
    )
    async def approval_resource(approval_id: str, ctx: Context) -> str:
        params = _validated(ApprovalRequest, {"approval_id": approval_id})
        return json.dumps(
            await _execute(ctx, "changes.status", params),
            ensure_ascii=False,
        )


def _register_prompts(mcp: FastMCP) -> None:
    @mcp.prompt(description="Analyze a bounded set of Odoo records without inventing fields.")
    def analyze_records(model: str, objective: str) -> str:
        return (
            f"Analyze Odoo model {model} for this objective: {objective}. "
            "First describe the model, then search with an explicit field list and bounded limit. "
            "State the domain, company, date range, currency, and missing data. "
            "Do not mutate records."
        )

    @mcp.prompt(description="Summarize one Odoo record with evidence.")
    def summarize_record(model: str, record_id: int) -> str:
        return (
            f"Read Odoo record {model} #{record_id}. Summarize only returned fields, "
            "cite the record ID, and distinguish facts from inferences."
        )

    @mcp.prompt(description="Prepare a safe Odoo change plan.")
    def prepare_change_plan(objective: str) -> str:
        return (
            f"Prepare this Odoo change: {objective}. Inspect schema and current records first. "
            "Call the matching preview tool with a unique idempotency key. "
            "A preview is not execution. Present target count, redacted diff, risk, and expiry; "
            "ask a human to approve it in Odoo. "
            "Execute only by approval ID after approval."
        )

    @mcp.prompt(description="Investigate an Odoo policy or access denial safely.")
    def investigate_access_denial(operation: str, model: str) -> str:
        return (
            f"Investigate why operation {operation} on {model} was denied. "
            "Check whoami, list_models, and describe_model. Do not attempt alternate models, "
            "fields, methods, or domains to bypass the denial. "
            "Report the missing policy or Odoo right."
        )

    @mcp.prompt(description="Review sales performance with explicit currency handling.")
    def sales_review(date_from: str, date_to: str) -> str:
        return (
            f"Review sales from {date_from} through {date_to}. "
            "Use the sales snapshot and drill down "
            "only through policy-visible records. Keep unlike currencies separate."
        )

    @mcp.prompt(description="Review receivables without combining unlike currencies.")
    def receivables_review(as_of: str) -> str:
        return (
            f"Review open receivables as of {as_of}. Use the aging summary, separate currencies, "
            "identify concentration and overdue risk, and do not post or reconcile entries."
        )
