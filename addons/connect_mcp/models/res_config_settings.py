from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    connect_mcp_enabled = fields.Boolean(
        string="Enable MCP Connector API",
        config_parameter="connect_mcp.enabled",
        default=True,
    )
    connect_mcp_max_payload_bytes = fields.Integer(
        string="Maximum JSON Payload Bytes",
        config_parameter="connect_mcp.max_payload_bytes",
        default=2 * 1024 * 1024,
    )
    connect_mcp_max_binary_bytes = fields.Integer(
        string="Maximum Binary/Report Bytes",
        config_parameter="connect_mcp.max_binary_bytes",
        default=5 * 1024 * 1024,
    )
    connect_mcp_audit_retention_days = fields.Integer(
        string="Audit Retention (days)",
        config_parameter="connect_mcp.audit_retention_days",
        default=90,
    )
    connect_mcp_approval_retention_days = fields.Integer(
        string="Approval Retention (days)",
        config_parameter="connect_mcp.approval_retention_days",
        default=30,
    )
