import json
import uuid

from odoo import http
from odoo.http import request
from odoo.tools.misc import str2bool


SECURITY_HEADERS = [
    ("Cache-Control", "no-store"),
    ("Pragma", "no-cache"),
    ("X-Content-Type-Options", "nosniff"),
]


class ConnectMcpController(http.Controller):
    @http.route(
        "/connect_mcp/v1/health",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        save_session=False,
    )
    def health(self):
        return self._json_response(
            {
                "status": "ok",
                "service": "connect_mcp",
                "api_version": "v1",
            },
            200,
        )

    @http.route(
        "/connect_mcp/v1/identity",
        type="http",
        auth="mcp",
        methods=["GET"],
        csrf=False,
        save_session=False,
    )
    def identity(self):
        request_id = self._request_id()
        if not self._enabled():
            return self._error(
                request_id,
                "service_disabled",
                "The MCP control API is disabled.",
                503,
                retryable=True,
            )
        access, response = self._access(request_id)
        if response:
            return response
        user = request.env.user
        return self._response(
            {
                "ok": True,
                "request_id": request_id,
                "data": {
                    "database": request.db,
                    "user_id": user.id,
                    "login": user.login,
                    "name": user.name,
                    "profile": access.profile_id.code,
                },
            },
            200,
        )

    @http.route(
        "/connect_mcp/v1/capabilities",
        type="http",
        auth="mcp",
        methods=["GET"],
        csrf=False,
        save_session=False,
    )
    def capabilities(self):
        request_id = self._request_id()
        if not self._enabled():
            return self._error(
                request_id,
                "service_disabled",
                "The MCP connector API is disabled.",
                503,
                retryable=True,
            )
        access, response = self._access(request_id)
        if response:
            return response
        body, status = request.env["connect.mcp.service"].execute_request(
            access,
            "capabilities",
            {},
            request_id,
            remote_ip=self._remote_ip(),
            user_agent=request.httprequest.headers.get("User-Agent", ""),
        )
        return self._response(body, status)

    @http.route(
        "/connect_mcp/v1/events/ticket",
        type="http",
        auth="mcp",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def event_ticket(self):
        request_id = self._request_id()
        if not self._enabled():
            return self._error(
                request_id,
                "service_disabled",
                "The MCP control API is disabled.",
                503,
                retryable=True,
            )
        access, response = self._access(request_id)
        if response:
            return response
        token = request.env["connect.mcp.event.ticket"].sudo()._issue(access)
        return self._response(
            {
                "ok": True,
                "request_id": request_id,
                "data": {
                    "ticket": token,
                    "expires_in": request.env["connect.mcp.event.ticket"]._ttl_seconds(),
                },
            },
            201,
        )

    @http.route(
        "/connect_mcp/v1/execute",
        type="http",
        auth="mcp",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def execute(self):
        request_id = self._request_id()
        if not self._enabled():
            return self._error(
                request_id,
                "service_disabled",
                "The MCP connector API is disabled.",
                503,
                retryable=True,
            )
        max_payload = int(
            request.env["ir.config_parameter"].sudo().get_param(
                "connect_mcp.max_payload_bytes",
                str(2 * 1024 * 1024),
            )
        )
        content_length = request.httprequest.content_length
        if content_length is not None and content_length > max_payload:
            return self._error(
                request_id,
                "payload_too_large",
                "The request body exceeds the configured limit.",
                413,
            )
        access, response = self._access(request_id)
        if response:
            return response
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True))
        except Exception:  # noqa: BLE001 - malformed protocol input
            return self._error(request_id, "invalid_json", "The request body is not valid JSON.", 400)
        if not isinstance(payload, dict):
            return self._error(request_id, "invalid_request", "The JSON body must be an object.", 400)
        operation = payload.get("operation")
        params = payload.get("params", {})
        if not isinstance(operation, str) or not operation:
            return self._error(request_id, "invalid_request", "Operation is required.", 400)
        if not isinstance(params, dict):
            return self._error(request_id, "invalid_request", "Params must be an object.", 400)
        body, status = request.env["connect.mcp.service"].execute_request(
            access,
            operation,
            params,
            request_id,
            remote_ip=self._remote_ip(),
            user_agent=request.httprequest.headers.get("User-Agent", ""),
        )
        encoded_size = len(json.dumps(body, ensure_ascii=False, default=str).encode())
        if encoded_size > max_payload:
            body = {
                "ok": False,
                "request_id": request_id,
                "error": {
                    "code": "response_too_large",
                    "message": "The response exceeds the configured limit.",
                    "retryable": False,
                },
            }
            status = 413
        return self._response(body, status)

    def _access(self, request_id):
        access, error = request.env["connect.mcp.access"]._for_user(request.env.user)
        if error:
            return access, self._error(
                request_id,
                error,
                "MCP access is not available for this Odoo user.",
                403,
            )
        return access, False

    def _enabled(self):
        value = request.env["ir.config_parameter"].sudo().get_param(
            "connect_mcp.enabled",
            "True",
        )
        return str2bool(value, True)

    def _request_id(self):
        value = request.httprequest.headers.get("X-Request-ID", "")
        if value:
            try:
                return str(uuid.UUID(value))
            except ValueError:
                pass
        return str(uuid.uuid4())

    def _remote_ip(self):
        return request.httprequest.remote_addr or ""

    def _error(
        self,
        request_id,
        code,
        message,
        status,
        *,
        retryable=False,
    ):
        return self._json_response(
            {
                "ok": False,
                "request_id": request_id,
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                },
            },
            status,
        )

    def _response(self, body, status):
        return self._json_response(body, status)

    def _json_response(self, body, status):
        response = request.make_response(
            json.dumps(body, ensure_ascii=False, default=str),
            headers=[("Content-Type", "application/json; charset=utf-8"), *SECURITY_HEADERS],
        )
        response.status_code = status
        return response
