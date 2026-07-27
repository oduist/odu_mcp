import json
import logging
import uuid

from odoo import http
from odoo.http import request
from odoo.tools.misc import str2bool


_logger = logging.getLogger(__name__)

SECURITY_HEADERS = [
    ("Cache-Control", "no-store"),
    ("Pragma", "no-cache"),
    ("X-Content-Type-Options", "nosniff"),
]


class OdooMcpController(http.Controller):
    @http.route(
        "/odoo_mcp/v1/health",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        save_session=False,
        readonly=True,
    )
    def health(self):
        return request.make_json_response(
            {
                "status": "ok",
                "service": "odoo_mcp_control",
                "api_version": "v1",
            },
            headers=SECURITY_HEADERS,
        )

    @http.route(
        "/odoo_mcp/v1/capabilities",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        save_session=False,
        readonly=True,
    )
    def capabilities(self):
        request_id = self._request_id()
        credential, response = self._authenticate(request_id)
        if response:
            return response
        body, status = request.env["odoo.mcp.service"].execute_request(
            credential,
            "capabilities",
            {},
            request_id,
            remote_ip=self._remote_ip(),
            user_agent=request.httprequest.headers.get("User-Agent", ""),
        )
        return self._response(body, status)

    @http.route(
        "/odoo_mcp/v1/execute",
        type="http",
        auth="public",
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
                "odoo_mcp_control.max_payload_bytes",
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
        credential, response = self._authenticate(request_id)
        if response:
            return response
        try:
            payload = request.get_json_data()
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
        body, status = request.env["odoo.mcp.service"].execute_request(
            credential,
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

    def _authenticate(self, request_id):
        authorization = request.httprequest.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            return request.env["odoo.mcp.credential"], self._error(
                request_id,
                "authentication_required",
                "A connector bearer token is required.",
                401,
                authenticate=True,
            )
        credential, error = request.env["odoo.mcp.credential"]._authenticate(
            token.strip(),
            self._remote_ip(),
        )
        if error:
            _logger.warning(
                "MCP connector authentication rejected request_id=%s reason=%s ip=%s",
                request_id,
                error,
                self._remote_ip(),
            )
            return credential, self._error(
                request_id,
                error,
                "Connector authentication failed.",
                401 if error != "ip_not_allowed" else 403,
                authenticate=error != "ip_not_allowed",
            )
        return credential, False

    def _enabled(self):
        value = request.env["ir.config_parameter"].sudo().get_param(
            "odoo_mcp_control.enabled",
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
        authenticate=False,
    ):
        headers = list(SECURITY_HEADERS)
        if authenticate:
            headers.append(("WWW-Authenticate", 'Bearer realm="odoo-mcp-connector"'))
        return request.make_json_response(
            {
                "ok": False,
                "request_id": request_id,
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                },
            },
            headers=headers,
            status=status,
        )

    def _response(self, body, status):
        return request.make_json_response(body, headers=SECURITY_HEADERS, status=status)
