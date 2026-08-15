import hashlib
import secrets

from odoo import api, fields, models


class OdooMcpEventTicket(models.Model):
    _name = "odoo.mcp.event.ticket"
    _description = "MCP Event Ticket"
    _order = "create_date desc"

    token_hash = fields.Char(required=True, readonly=True, index=True)
    access_id = fields.Many2one(
        "odoo.mcp.access",
        required=True,
        readonly=True,
        ondelete="cascade",
        index=True,
    )
    expires_at = fields.Datetime(required=True, readonly=True, index=True)
    last_used_at = fields.Datetime(readonly=True)

    _token_hash_unique = models.Constraint(
        "UNIQUE(token_hash)",
        "The MCP event ticket must be unique.",
    )

    @api.model
    def _ttl_seconds(self):
        value = self.env["ir.config_parameter"].sudo().get_param(
            "odoo_mcp_control.event_ticket_ttl_seconds",
            "600",
        )
        return min(max(int(value), 60), 3600)

    @api.model
    def _issue(self, access):
        token = secrets.token_urlsafe(32)
        now = fields.Datetime.now()
        self.sudo().create(
            {
                "token_hash": self._digest(token),
                "access_id": access.id,
                "expires_at": fields.Datetime.add(now, seconds=self._ttl_seconds()),
            }
        )
        return token

    @api.model
    def _check(self, token):
        if not isinstance(token, str) or not token:
            return self.browse()
        now = fields.Datetime.now()
        ticket = self.sudo().search(
            [
                ("token_hash", "=", self._digest(token)),
                ("expires_at", ">=", now),
            ],
            limit=1,
        )
        access = ticket.access_id
        if not ticket or not access.active or not access.profile_id.active or not access.user_id.active:
            return self.browse()
        ticket.write({"last_used_at": now})
        return ticket

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @api.autovacuum
    def _gc_event_tickets(self):
        self.sudo().search([("expires_at", "<", fields.Datetime.now())], limit=5000).unlink()
