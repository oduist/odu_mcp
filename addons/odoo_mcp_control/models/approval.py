import hashlib
import json
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class OdooMcpApproval(models.Model):
    _name = "odoo.mcp.approval"
    _description = "MCP Change Approval"
    _order = "create_date desc"
    _rec_name = "request_uid"

    request_uid = fields.Char(required=True, default=lambda self: str(uuid.uuid4()), readonly=True, index=True)
    access_id = fields.Many2one(
        "odoo.mcp.access",
        required=True,
        ondelete="restrict",
        readonly=True,
        index=True,
    )
    profile_id = fields.Many2one(related="access_id.profile_id", store=True, readonly=True)
    user_id = fields.Many2one(related="access_id.user_id", store=True, readonly=True)
    action = fields.Selection(
        [
            ("record.create", "Create Records"),
            ("record.update", "Update Records"),
            ("record.delete", "Delete Records"),
            ("message.post", "Post Message"),
            ("activity.schedule", "Schedule Activity"),
            ("attachment.create", "Create Attachment"),
            ("method.call", "Call Allowed Method"),
        ],
        required=True,
        readonly=True,
        index=True,
    )
    model_name = fields.Char(required=True, readonly=True, index=True)
    payload_json = fields.Text(required=True, readonly=True, groups="odoo_mcp_control.group_mcp_manager")
    payload_hash = fields.Char(required=True, readonly=True, index=True)
    idempotency_key = fields.Char(required=True, readonly=True)
    risk_level = fields.Selection(
        [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("critical", "Critical")],
        required=True,
        readonly=True,
        index=True,
    )
    summary = fields.Char(required=True, readonly=True)
    target_count = fields.Integer(readonly=True)
    diff_json = fields.Text(readonly=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("executing", "Executing"),
            ("executed", "Executed"),
            ("expired", "Expired"),
            ("failed", "Failed"),
        ],
        required=True,
        default="pending",
        readonly=True,
        index=True,
    )
    expires_at = fields.Datetime(required=True, readonly=True, index=True)
    approved_by = fields.Many2one("res.users", readonly=True)
    approved_at = fields.Datetime(readonly=True)
    rejected_by = fields.Many2one("res.users", readonly=True)
    rejected_at = fields.Datetime(readonly=True)
    executed_at = fields.Datetime(readonly=True)
    result_json = fields.Text(readonly=True)
    error_message = fields.Text(readonly=True)

    _request_uid_unique = models.Constraint(
        "UNIQUE(request_uid)",
        "The approval request identifier must be unique.",
    )
    _idempotency_unique = models.Constraint(
        "UNIQUE(access_id, idempotency_key)",
        "The idempotency key has already been used by this MCP access assignment.",
    )

    def action_approve(self):
        if not self.env.user._has_group("odoo_mcp_control.group_mcp_manager"):
            raise AccessError(_("Only MCP managers can approve change plans."))
        for approval in self:
            approval.lock_for_update()
            if approval.state != "pending":
                raise UserError(_("Only pending plans can be approved."))
            if approval.expires_at <= fields.Datetime.now():
                approval._system_write({"state": "expired"})
                raise UserError(_("This change plan has expired."))
            approval._system_write(
                {
                    "state": "approved",
                    "approved_by": self.env.user.id,
                    "approved_at": fields.Datetime.now(),
                }
            )

    def action_reject(self):
        if not self.env.user._has_group("odoo_mcp_control.group_mcp_manager"):
            raise AccessError(_("Only MCP managers can reject change plans."))
        for approval in self:
            approval.lock_for_update()
            if approval.state not in {"pending", "approved"}:
                raise UserError(_("Only pending or approved plans can be rejected."))
            approval._system_write(
                {
                    "state": "rejected",
                    "rejected_by": self.env.user.id,
                    "rejected_at": fields.Datetime.now(),
                }
            )

    def _system_write(self, values):
        result = super(OdooMcpApproval, self.with_context(mcp_approval_system_write=True)).write(
            values
        )
        if "state" in values:
            for approval in self:
                approval.access_id._publish_resource_update(
                    f"odoo://approval/{approval.request_uid}"
                )
        return result

    def write(self, values):
        if self.env.context.get("mcp_approval_system_write"):
            return super().write(values)
        protected = set(self._fields) - {"display_name"}
        if protected & set(values):
            raise AccessError(_("MCP change plans are immutable. Use the approval actions."))
        return super().write(values)

    def unlink(self):
        if not self.env.context.get("mcp_retention_cleanup") or not self.env.is_system():
            raise AccessError(_("MCP change plans can only be removed by retention cleanup."))
        return super().unlink()

    @classmethod
    def _canonical_payload(cls, payload):
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def _payload_digest(cls, payload_json):
        return hashlib.sha256(payload_json.encode()).hexdigest()

    @classmethod
    def _json_result(cls, value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _public_dict(self):
        self.ensure_one()
        result = json.loads(self.result_json) if self.result_json else None
        diff = json.loads(self.diff_json) if self.diff_json else []
        return {
            "approval_id": self.request_uid,
            "state": self.state,
            "action": self.action,
            "model": self.model_name,
            "risk_level": self.risk_level,
            "summary": self.summary,
            "target_count": self.target_count,
            "diff": diff,
            "expires_at": fields.Datetime.to_string(self.expires_at),
            "result": result,
            "error": self.error_message or None,
            "next_step": (
                "Approve this plan in Odoo, then call odoo_execute_approved_change."
                if self.state == "pending"
                else None
            ),
        }

    @api.autovacuum
    def _gc_approvals(self):
        now = fields.Datetime.now()
        expired = self.sudo().search(
            [("state", "in", ["pending", "approved"]), ("expires_at", "<", now)],
            limit=5000,
        )
        expired._system_write({"state": "expired"})
        retention_days = int(
            self.env["ir.config_parameter"].sudo().get_param(
                "odoo_mcp_control.approval_retention_days", "30"
            )
        )
        cutoff = fields.Datetime.subtract(now, days=max(1, retention_days))
        old = self.sudo().search(
            [
                ("state", "in", ["executed", "rejected", "expired", "failed"]),
                ("create_date", "<", cutoff),
            ],
            limit=5000,
        )
        old.with_context(mcp_retention_cleanup=True).unlink()
