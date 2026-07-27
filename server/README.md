# Odoo Agent MCP

Security-first MCP server for Odoo 19 and the companion `odoo_mcp_control` addon.

## Why two components

The MCP process terminates the client protocol and never receives direct ORM credentials. The Odoo addon remains the authority for users, companies, model/field policies, ACL, record rules, approvals, execution, and audit.

```text
MCP client
    │  stdio or Streamable HTTP
    ▼
odoo-agent-mcp
    │  dedicated connector bearer token
    ▼
odoo_mcp_control
    │  non-sudo Odoo user environment
    ▼
Odoo ORM + ACL + record rules
```

## Requirements

- Python 3.11–3.13
- Odoo 19 with `odoo_mcp_control`
- One connector credential issued in Odoo

The MCP SDK is pinned to stable `1.28.1`. SDK 2.0 was still a release candidate when version 1.0 of this project was prepared.

## Local stdio

```bash
cp .env.example .env
export ODOO_MCP_ODOO_URL=https://odoo.example.com
export ODOO_MCP_CONNECTOR_TOKEN='omcp_...'
export ODOO_MCP_TRANSPORT=stdio
uv sync
uv run odoo-agent-mcp
```

Example Codex/Claude-style configuration:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/odu_mcp/server", "run", "odoo-agent-mcp"],
      "env": {
        "ODOO_MCP_ODOO_URL": "https://odoo.example.com",
        "ODOO_MCP_CONNECTOR_TOKEN": "omcp_..."
      }
    }
  }
}
```

## Streamable HTTP

```bash
export ODOO_MCP_TRANSPORT=streamable-http
export ODOO_MCP_HOST=0.0.0.0
export ODOO_MCP_PORT=8000
export ODOO_MCP_AUTH_MODE=static-token
export ODOO_MCP_STATIC_TOKENS='replace-with-a-client-token'
uv run odoo-agent-mcp
```

Clients connect to `http://localhost:8000/mcp`. Use TLS at the reverse proxy. `AUTH_MODE=none` is rejected for non-loopback HTTP.

HTTP health endpoints are available at `/healthz` (process liveness) and
`/readyz` (includes authenticated Odoo connector availability).

For OAuth resource-server mode:

```bash
export ODOO_MCP_AUTH_MODE=oauth
export ODOO_MCP_OAUTH_ISSUER_URL=https://auth.example.com
export ODOO_MCP_RESOURCE_SERVER_URL=https://mcp.example.com
export ODOO_MCP_OAUTH_AUDIENCE=https://mcp.example.com
export ODOO_MCP_JWKS_URL=https://auth.example.com/.well-known/jwks.json
export ODOO_MCP_REQUIRED_SCOPES='odoo:read'
```

## Tool groups

`core` is always enabled. Additional groups are comma-separated:

```bash
export ODOO_MCP_TOOL_GROUPS=core,write,collaboration,documents,sales,accounting,inventory,projects,hr
```

- `core`: identity, models, schema, search, read, count, aggregate, approval status/execution.
- `write`: create/update/delete/method previews.
- `collaboration`: message/activity previews.
- `documents`: attachments and reports.
- Domain groups: safe read-only management summaries.

Enabling a tool group does not grant Odoo access. The connector profile must independently allow the model, operation, fields, and forced domain.

## Safe write workflow

1. Call a `odoo_preview_*` tool with an idempotency key.
2. Inspect the exact target count and redacted diff.
3. Approve the plan in Odoo.
4. Call `odoo_execute_approved_change` with only the approval ID.

There is no `force` parameter. Executing an already completed plan returns the stored result without repeating the mutation.

## Tests

```bash
uv sync --extra test
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=odoo_agent_mcp
```

## Security notes

- Never use an Odoo administrator as the connector user.
- Do not enable broad write policies or generic methods.
- Keep HTTP auth and the Odoo connector token separate.
- Put the connector token and client tokens in a secret manager.
- Review the Odoo approval inbox and audit log.
- Keep `VERIFY_TLS=true` outside isolated development environments.
