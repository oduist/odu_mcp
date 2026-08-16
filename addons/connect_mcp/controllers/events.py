import json

from odoo import http
from odoo.addons.bus.models.bus import dispatch
from odoo.http import request


class ConnectMcpEventController(http.Controller):
    @http.route(
        "/connect_mcp/v1/events",
        type="http",
        auth="mcp_event",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def events(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except (TypeError, ValueError):
            return self._response({"error": "invalid_json"}, 400)
        last = payload.get("last", 0) if isinstance(payload, dict) else None
        if not isinstance(last, int) or isinstance(last, bool) or last < 0:
            return self._response({"error": "invalid_last"}, 400)
        channel = request.env.context.get("connect_mcp_event_channel")
        if not channel:
            return self._response({"error": "missing_event_channel"}, 403)

        request.cr.close()
        request._cr = None
        notifications = dispatch.poll(request.db, [channel], last, {})
        next_last = max(
            [notification.get("id", last) for notification in notifications],
            default=last,
        )
        return self._response(
            {"notifications": notifications, "last": next_last},
            200,
        )

    def _response(self, body, status):
        response = request.make_response(
            json.dumps(body, ensure_ascii=False),
            headers=[
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        response.status_code = status
        return response
