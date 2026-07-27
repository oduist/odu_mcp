import hashlib
import hmac
import ipaddress
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError


class OdooMcpCredential(models.Model):
    _name = "odoo.mcp.credential"
    _description = "MCP Connector Credential"
    _order = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    profile_id = fields.Many2one(
        "odoo.mcp.profile",
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
    key_prefix = fields.Char(readonly=True, copy=False, index=True)
    secret_digest = fields.Char(readonly=True, copy=False, groups="odoo_mcp_control.group_mcp_manager")
    secret_hint = fields.Char(readonly=True, copy=False)
    expires_at = fields.Datetime()
    last_used_at = fields.Datetime(readonly=True, copy=False)
    allowed_ip_networks = fields.Text(
        help="Optional IPv4/IPv6 CIDR networks, separated by commas or new lines.",
    )
    request_count = fields.Integer(readonly=True, copy=False, default=0)
    failure_count = fields.Integer(readonly=True, copy=False, default=0)
    revoked_at = fields.Datetime(readonly=True, copy=False)

    _key_prefix_unique = models.Constraint(
        "UNIQUE(key_prefix)",
        "The MCP credential prefix must be unique.",
    )

    @api.constrains("allowed_ip_networks")
    def _check_allowed_ip_networks(self):
        for credential in self:
            for value in credential._network_values():
                try:
                    ipaddress.ip_network(value, strict=False)
                except ValueError as exc:
                    raise ValidationError(_("Invalid IP network: %s", value)) from exc

    @api.constrains("expires_at")
    def _check_expiration(self):
        for credential in self:
            if credential.expires_at and credential.expires_at <= fields.Datetime.now():
                raise ValidationError(_("Credential expiration must be in the future."))

    def _network_values(self):
        self.ensure_one()
        return [
            value.strip()
            for value in (self.allowed_ip_networks or "").replace(",", "\n").splitlines()
            if value.strip()
        ]

    def _ip_is_allowed(self, remote_ip):
        self.ensure_one()
        networks = self._network_values()
        if not networks:
            return True
        try:
            address = ipaddress.ip_address(remote_ip)
        except ValueError:
            return False
        return any(
            address in ipaddress.ip_network(network, strict=False)
            for network in networks
        )

    def _generate_secret(self):
        self.ensure_one()
        self.check_access("write")
        prefix = secrets.token_hex(5)
        token = f"omcp_{prefix}_{secrets.token_urlsafe(32)}"
        self.write(
            {
                "key_prefix": prefix,
                "secret_digest": hashlib.sha256(token.encode()).hexdigest(),
                "secret_hint": f"omcp_{prefix}_…{token[-4:]}",
                "active": True,
                "revoked_at": False,
            }
        )
        return token

    def action_generate_secret(self):
        self.ensure_one()
        if not self.env.user._has_group("odoo_mcp_control.group_mcp_manager"):
            raise AccessError(_("Only MCP managers can issue connector secrets."))
        token = self._generate_secret()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connector secret — copy it now"),
                "message": token,
                "sticky": True,
                "type": "warning",
            },
        }

    def action_revoke(self):
        self.ensure_one()
        if not self.env.user._has_group("odoo_mcp_control.group_mcp_manager"):
            raise AccessError(_("Only MCP managers can revoke connector secrets."))
        self.write(
            {
                "active": False,
                "secret_digest": False,
                "revoked_at": fields.Datetime.now(),
            }
        )

    @api.model
    def _authenticate(self, token, remote_ip):
        if not isinstance(token, str) or not token.startswith("omcp_"):
            return self.browse(), "invalid_credential"
        parts = token.split("_", 2)
        if len(parts) != 3 or not parts[1]:
            return self.browse(), "invalid_credential"
        credential = self.sudo().search(
            [("key_prefix", "=", parts[1]), ("active", "=", True)],
            limit=1,
        )
        if not credential or not credential.secret_digest:
            return self.browse(), "invalid_credential"
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not hmac.compare_digest(digest, credential.secret_digest):
            return self.browse(), "invalid_credential"
        now = fields.Datetime.now()
        if credential.expires_at and credential.expires_at <= now:
            return self.browse(), "expired_credential"
        if not credential.user_id.active or not credential.profile_id.active:
            return self.browse(), "inactive_credential"
        if not credential._ip_is_allowed(remote_ip):
            return self.browse(), "ip_not_allowed"
        return credential, False

    def _check_quota(self):
        self.ensure_one()
        now = fields.Datetime.now()
        minute_start = fields.Datetime.subtract(now, minutes=1)
        profile = self.profile_id
        Audit = self.env["odoo.mcp.audit.log"].sudo()
        minute_count = Audit.search_count(
            [("credential_id", "=", self.id), ("create_date", ">=", minute_start)]
        )
        if minute_count >= profile.rate_limit_per_minute:
            return "rate_limit_exceeded"
        if profile.daily_quota:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            daily_count = Audit.search_count(
                [("credential_id", "=", self.id), ("create_date", ">=", day_start)]
            )
            if daily_count >= profile.daily_quota:
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
