from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    odoo_mcp_enabled = fields.Boolean(
        string="Enable MCP Connector API",
        config_parameter="odoo_mcp_control.enabled",
        default=True,
    )
    odoo_mcp_max_payload_bytes = fields.Integer(
        string="Maximum JSON Payload Bytes",
        config_parameter="odoo_mcp_control.max_payload_bytes",
        default=2 * 1024 * 1024,
    )
    odoo_mcp_max_binary_bytes = fields.Integer(
        string="Maximum Binary/Report Bytes",
        config_parameter="odoo_mcp_control.max_binary_bytes",
        default=5 * 1024 * 1024,
    )
    odoo_mcp_audit_retention_days = fields.Integer(
        string="Audit Retention (days)",
        config_parameter="odoo_mcp_control.audit_retention_days",
        default=90,
    )
    odoo_mcp_approval_retention_days = fields.Integer(
        string="Approval Retention (days)",
        config_parameter="odoo_mcp_control.approval_retention_days",
        default=30,
    )
