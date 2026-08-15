from odoo import http
from odoo.addons.bus.websocket import WebsocketConnectionHandler
from odoo.http import request


class OdooMcpEventController(http.Controller):
    @http.route(
        "/odoo_mcp/v1/events",
        type="http",
        auth="mcp_event",
        methods=["GET"],
        csrf=False,
        save_session=True,
        websocket=True,
    )
    def events(self, version=None):
        return WebsocketConnectionHandler.open_connection(request, version)
