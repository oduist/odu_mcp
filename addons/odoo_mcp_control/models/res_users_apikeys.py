from odoo import api, fields, models
from odoo.addons.base.models.res_users import INDEX_SIZE, KEY_CRYPT_CONTEXT


class ResUsersApikeysDescription(models.TransientModel):
    _inherit = "res.users.apikeys.description"

    scope_mode = fields.Selection(
        [
            ("mcp", "MCP only"),
            ("global", "All APIs"),
        ],
        required=True,
        default="mcp",
        string="Access",
        help=(
            "MCP-only keys can authenticate only to the MCP control API. "
            "All APIs creates a standard unrestricted Odoo API key."
        ),
    )

    def make_key(self):
        context = dict(self.env.context)
        if self.scope_mode == "mcp":
            context["odoo_mcp_api_key_scope"] = "mcp"
        return super(ResUsersApikeysDescription, self.with_context(context)).make_key()


class ResUsersApikeys(models.Model):
    _inherit = "res.users.apikeys"

    @api.model
    def _check_mcp_credentials(self, key):
        """Authenticate only an exact MCP-scoped key, excluding global keys."""
        if not isinstance(key, str) or not (INDEX_SIZE <= len(key) <= 512):
            return False
        self.env.cr.execute(
            """
            SELECT api_key.user_id, api_key.key
              FROM res_users_apikeys AS api_key
              JOIN res_users AS users ON users.id = api_key.user_id
             WHERE users.active
               AND api_key.scope = 'mcp'
               AND api_key.index = %s
               AND (
                    api_key.expiration_date IS NULL
                    OR api_key.expiration_date >= now() AT TIME ZONE 'utc'
               )
            """,
            [key[:INDEX_SIZE]],
        )
        for user_id, stored_key in self.env.cr.fetchall():
            if KEY_CRYPT_CONTEXT.verify(key, stored_key):
                return user_id
        return False

    def _generate(self, scope, name, expiration_date):
        scope = scope or self.env.context.get("odoo_mcp_api_key_scope")
        return super()._generate(scope, name, expiration_date)
