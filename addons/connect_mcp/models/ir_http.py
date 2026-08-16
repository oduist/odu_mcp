import re

from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import Unauthorized

from odoo import models
from odoo.http import request


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _auth_method_mcp(cls):
        header = request.httprequest.headers.get("Authorization", "")
        match = re.fullmatch(r"Bearer\s+(.+)", header, re.IGNORECASE)
        if not match:
            raise Unauthorized(
                "An MCP-scoped Odoo API key is required.",
                www_authenticate=WWWAuthenticate("Bearer"),
            )

        user_id = request.env["res.users.apikeys"]._check_mcp_credentials(match.group(1))
        if not user_id:
            raise Unauthorized(
                "The MCP API key is invalid or expired.",
                www_authenticate=WWWAuthenticate("Bearer"),
            )
        if request.session.uid and request.session.uid != user_id:
            raise Unauthorized(
                "The current session does not match the MCP API key.",
                www_authenticate=WWWAuthenticate("Bearer"),
            )

        request.uid = user_id
        request.context = request.env["res.users"].context_get()

    @classmethod
    def _auth_method_mcp_event(cls):
        header = request.httprequest.headers.get("Authorization", "")
        match = re.fullmatch(r"Bearer\s+(.+)", header, re.IGNORECASE)
        ticket = (
            request.env["connect.mcp.event.ticket"].sudo()._check(match.group(1))
            if match
            else request.env["connect.mcp.event.ticket"].browse()
        )
        if not ticket:
            raise Unauthorized(
                "The MCP event ticket is invalid or expired.",
                www_authenticate=WWWAuthenticate("Bearer"),
            )

        request.uid = request.env.ref("base.public_user").id
        context = dict(request.env["res.users"].context_get())
        context["connect_mcp_event_channel"] = ticket.access_id.event_channel
        request.context = context
