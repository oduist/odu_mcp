from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class ConnectMcpAuditLog(models.Model):
    _name = "connect.mcp.audit.log"
    _description = "MCP Audit Log"
    _order = "create_date desc, id desc"
    _rec_name = "request_id"

    request_id = fields.Char(required=True, readonly=True, index=True)
    access_id = fields.Many2one(
        "connect.mcp.access",
        ondelete="set null",
        readonly=True,
        index=True,
    )
    profile_id = fields.Many2one(
        "connect.mcp.profile",
        ondelete="set null",
        readonly=True,
        index=True,
    )
    user_id = fields.Many2one("res.users", ondelete="set null", readonly=True, index=True)
    approval_id = fields.Many2one("connect.mcp.approval", ondelete="set null", readonly=True, index=True)
    operation = fields.Char(required=True, readonly=True, index=True)
    model_name = fields.Char(readonly=True, index=True)
    input_hash = fields.Char(readonly=True)
    input_summary = fields.Char(readonly=True)
    target_ids_json = fields.Text(readonly=True)
    outcome = fields.Selection(
        [("success", "Success"), ("denied", "Denied"), ("error", "Error")],
        required=True,
        readonly=True,
        index=True,
    )
    status_code = fields.Integer(readonly=True)
    error_code = fields.Char(readonly=True, index=True)
    error_class = fields.Char(readonly=True)
    duration_ms = fields.Integer(readonly=True)
    remote_ip = fields.Char(readonly=True)
    user_agent = fields.Char(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get("mcp_audit_system_create"):
            raise AccessError(_("MCP audit rows may only be created by the connector service."))
        return super().create(vals_list)

    def write(self, values):
        raise AccessError(_("MCP audit rows are immutable."))

    def unlink(self):
        if not self.env.context.get("mcp_retention_cleanup") or not self.env.is_system():
            raise AccessError(_("MCP audit rows can only be removed by retention cleanup."))
        return super().unlink()

    @api.autovacuum
    def _gc_audit_logs(self):
        retention_days = int(
            self.env["ir.config_parameter"].sudo().get_param(
                "connect_mcp.audit_retention_days", "90"
            )
        )
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=max(1, retention_days))
        old = self.sudo().search([("create_date", "<", cutoff)], limit=10000)
        old.with_context(mcp_retention_cleanup=True).unlink()
