# Odoo Addons

`connect_mcp` is the Odoo 15 control plane for the HTTP FastMCP sidecar.

Install it only on a fresh database. This version deliberately refuses to load
if the removed legacy credential schema is detected.

After installation:

1. Create an MCP profile and its model/field policies.
2. Assign the profile to an Odoo user under **Connect MCP > User Access**.
3. Have that user open their profile's API Keys dialog, create a key, and choose
   **MCP only**.
4. Configure the key as the bearer token in the user's MCP client.

For subscriptions, route `/connect_mcp/v1/events` to Odoo's evented worker in the
same way as the standard Odoo `/longpolling/poll` endpoint.
