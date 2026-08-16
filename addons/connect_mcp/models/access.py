import uuid

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ConnectMcpAccess(models.Model):
    _name = "connect.mcp.access"
    _description = "MCP User Access"
    _order = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    profile_id = fields.Many2one(
        "connect.mcp.profile",
        required=True,
        ondelete="restrict",
        index=True,
    )
    user_id = fields.Many2one(
        "res.users",
        required=True,
        ondelete="restrict",
        domain=[("active", "=", True)],
        index=True,
    )
    last_used_at = fields.Datetime(readonly=True, copy=False)
    request_count = fields.Integer(readonly=True, copy=False, default=0)
    failure_count = fields.Integer(readonly=True, copy=False, default=0)
    event_channel = fields.Char(
        required=True,
        readonly=True,
        copy=False,
        default=lambda self: f"connect_mcp_{uuid.uuid4().hex}",
        groups="connect_mcp.group_mcp_manager",
    )
    event_version = fields.Integer(readonly=True, copy=False, default=0)

    _sql_constraints = [
        (
            "user_unique",
            "UNIQUE(user_id)",
            "Each Odoo user may have only one MCP access assignment.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            """
            SELECT to_regclass('connect_mcp_credential') IS NOT NULL
                OR EXISTS (
                    SELECT 1 FROM ir_model WHERE model = 'connect.mcp.credential'
                )
            """
        )
        if self.env.cr.fetchone()[0]:
            raise ValidationError(
                _(
                    "Unsupported connect_mcp database detected: the obsolete "
                    "connect_mcp_credential table exists. Install this version on a fresh database."
                )
            )

    @api.model
    def _for_user(self, user):
        access = (
            self.sudo()
            .with_context(active_test=False)
            .search([("user_id", "=", user.id)], limit=1)
        )
        if not access:
            return self.browse(), "mcp_access_not_configured"
        if not access.active or not user.active or not access.profile_id.active:
            return self.browse(), "inactive_mcp_access"
        return access, False

    def _check_quota(self):
        self.ensure_one()
        now = fields.Datetime.now()
        minute_start = fields.Datetime.subtract(now, minutes=1)
        audit_model = self.env["connect.mcp.audit.log"].sudo()
        minute_count = audit_model.search_count(
            [("access_id", "=", self.id), ("create_date", ">=", minute_start)]
        )
        if minute_count >= self.profile_id.rate_limit_per_minute:
            return "rate_limit_exceeded"
        if self.profile_id.daily_quota:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            daily_count = audit_model.search_count(
                [("access_id", "=", self.id), ("create_date", ">=", day_start)]
            )
            if daily_count >= self.profile_id.daily_quota:
                return "daily_quota_exceeded"
        return False

    def _record_use(self, success):
        self.ensure_one()
        values = {
            "last_used_at": fields.Datetime.now(),
            "request_count": self.request_count + 1,
        }
        if not success:
            values["failure_count"] = self.failure_count + 1
        self.sudo().write(values)

    def _publish_resource_update(self, uri):
        self.ensure_one()
        if not isinstance(uri, str) or not uri.startswith("odoo://"):
            raise ValidationError(_("MCP event resources must use an odoo:// URI."))
        self.env.cr.execute(
            """
            UPDATE connect_mcp_access
               SET event_version = event_version + 1
             WHERE id = %s
         RETURNING event_version
            """,
            (self.id,),
        )
        version = self.env.cr.fetchone()[0]
        self.invalidate_cache(["event_version"])
        self.env["bus.bus"]._sendone(
            self.event_channel,
            "connect_mcp_resource_updated",
            {
                "type": "resource.updated",
                "uri": uri,
                "version": version,
            },
        )
        return version
