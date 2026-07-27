# Odu MCP

A security-first integration between Odoo 19 and AI agents using the Model
Context Protocol (MCP).

The project deliberately separates the trust boundary into two components:

- `addons/odoo_mcp_control` is the Odoo-side policy, approval, execution, and
  audit control plane.
- `server` is the Python MCP protocol server. It exposes bounded tools,
  resources, and prompts over stdio or Streamable HTTP.

The MCP process never receives direct database or unrestricted ORM access. Every
operation is revalidated inside Odoo under a dedicated non-superuser identity,
Odoo ACLs and record rules, an explicit model/field policy, and a forced domain.
Changes use preview → human approval → exactly-once execution.

## Repository layout

```text
addons/
  odoo_mcp_control/        Odoo 19 addon
server/                    Python MCP server
specs/
  odoo_mcp_control_spec.md Product and technical specification for the addon
  odoo_mcp_server_spec.md  Product and technical specification for the server
```

## Capabilities

- Explicit profiles for companies, models, fields, operations, methods, forced
  domains, record/batch limits, quotas, binary access, and feature groups.
- One-time connector secrets stored only as hashes, revocation, expiration,
  CIDR allowlists, and separate connector identities.
- Schema discovery, search/read/count/aggregation, attachment reads, reports,
  and bounded domain summaries.
- Preview-only create/update/delete/method/chatter/activity/attachment tools.
- Immutable, expiring approvals with idempotency and exactly-once execution.
- Immutable redacted audit records with request correlation IDs.
- MCP stdio and Streamable HTTP transports; static bearer or OIDC/JWT
  resource-server authentication.
- MCP tools, resources, prompts, structured errors, safe-read retries, response
  limits, circuit breaker, `/healthz`, and dependency-aware `/readyz`.

## Development

Test the Python server:

```bash
cd server
uv sync --extra test
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=odoo_agent_mcp
```

Install and test the addon using a clean Odoo 19 database:

```bash
odoo \
  --addons-path=/path/to/odoo/addons,/absolute/path/to/odu_mcp/addons \
  -d odu_mcp_test \
  -i odoo_mcp_control \
  --test-enable \
  --test-tags=/odoo_mcp_control \
  --stop-after-init
```

Configuration and deployment examples for the MCP process are in
[`server/README.md`](server/README.md). The normative requirements and
acceptance criteria live in [`specs/`](specs/).

## Security defaults

- Use a dedicated least-privilege Odoo user for every connector credential.
- Treat an empty model or field allowlist as no access, not wildcard access.
- Never enable generic ORM or arbitrary Python method execution.
- Keep MCP client authentication separate from the Odoo connector secret.
- Terminate TLS at a trusted reverse proxy and keep TLS verification enabled.
- Review approvals and audit logs, rotate credentials, and configure retention.

## Licensing

The Odoo addon declares `LGPL-3`. The standalone Python MCP server declares
`Apache-2.0`. Contributions must remain compatible with the license of the
component they modify.
