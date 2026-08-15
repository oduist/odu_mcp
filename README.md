# Connect MCP

Connect MCP connects MCP clients to Odoo 19 while keeping authorization, policy,
approval, and audit decisions inside Odoo.

The project has two deliberately separate components:

- `addons/connect_mcp` owns Odoo identities, MCP profiles, ACL and record
  rule enforcement, change approvals, execution, audit, and resource events.
- `addons/connect_mcp/deploy/connect_mcp_server` is a single-replica FastMCP
  v4 HTTP sidecar. It owns MCP protocol
  handling, connection pooling, per-user serialization, circuit breaking, and
  subscription delivery.

```text
MCP client
  |  Streamable HTTP + user's Connect MCP API key
  v
FastMCP sidecar (one replica)
  |  same key, forwarded only for the current request
  v
Connect MCP control API
  |  effective Odoo user + MCP profile + ACLs + record rules
  v
Odoo ORM
```

There is no stdio transport, connector identity, shared connector secret,
sidecar-issued client token, or direct database access.

## Documentation

- [Руководство пользователя](docs/user-guide.md)
- [Руководство администратора](docs/admin-guide.md)
- [Odoo addon reference](addons/README.md)
- [FastMCP sidecar reference](addons/connect_mcp/deploy/connect_mcp_server/README.md)
- [Control-plane specification](specs/connect_mcp_spec.md)
- [Sidecar specification](specs/connect_mcp_server_spec.md)

## Capabilities

- User-created Odoo API keys restricted to the `mcp` scope.
- One MCP access assignment and profile per Odoo user.
- Explicit model, field, operation, method, company, forced-domain, quota, and
  binary/report policies.
- Schema discovery, search, read, count, aggregation, attachments, reports, and
  bounded domain summaries.
- Preview-only create, update, delete, method, chatter, activity, and attachment
  tools.
- Immutable expiring approvals and exactly-once execution by approval ID.
- Immutable redacted audit records with request correlation IDs.
- HTTP health/readiness endpoints, safe-read retries, response limits, a
  transport-only circuit breaker, and one-operation-at-a-time enforcement per
  Odoo user.
- MCP `subscriptions/listen` updates for approval and MCP-executed record
  resources, backed by Odoo Bus WebSocket events and short-lived event tickets.

## Installation

Install the addon on a fresh Odoo 19 database:

```bash
odoo \
  --addons-path=/path/to/odoo/addons,/absolute/path/to/connect_addons_ng/addons \
  -d connect_addons_ng \
  -i connect_mcp \
  --stop-after-init
```

This release intentionally rejects databases that contain the obsolete
`connect.mcp.credential` schema. No migration or compatibility mode is provided.

Run the sidecar:

```bash
cd addons/connect_mcp/deploy/connect_mcp_server
uv sync
export CONNECT_MCP_ODOO_URL=https://odoo.example.com
export CONNECT_MCP_HOST=0.0.0.0
uv run connect-mcp-server
```

In Odoo, assign an MCP profile under **Connect MCP > User Access**. The user
then creates an API key in their own profile and selects **MCP only**. Configure
that key as the bearer token in their MCP client.

Odoo subscriptions require the standard Odoo evented worker and reverse-proxy
support for `/connect_mcp/v1/events`, just as Odoo's normal `/websocket` endpoint
does.

## Development

```bash
cd addons/connect_mcp/deploy/connect_mcp_server
uv sync --extra test
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest --cov=connect_mcp_server --cov-report=term-missing --cov-fail-under=95
```

Run addon tests only on a fresh database:

```bash
/path/to/odoo-bin \
  --addons-path=/path/to/odoo/addons,/absolute/path/to/connect_addons_ng/addons \
  -d connect_addons_ng_test \
  -i connect_mcp \
  --test-enable \
  --test-tags=/connect_mcp \
  --stop-after-init
```

## Deployment Constraints

- Run exactly one sidecar replica.
- Do not add Redis, sticky sessions, or distributed locks for this deployment.
- Terminate TLS at a trusted reverse proxy and keep Odoo TLS verification on.
- Do not use an administrator account as an MCP user.
- Treat an empty allowlist as no access, never as wildcard access.

The addon is `LGPL-3`; the standalone sidecar is `Apache-2.0`.
