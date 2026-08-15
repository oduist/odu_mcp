# Odoo Agent MCP Sidecar

This is the HTTP-only FastMCP v4 sidecar for `odoo_mcp_control`.

## Authentication

Every MCP client sends an Odoo API key created by that user with scope `mcp`.
The sidecar validates the key through `/odoo_mcp/v1/identity` and forwards the
same bearer key only on that user's individual Odoo calls. Authorization is
never stored in shared `httpx` client headers.

The sidecar does not issue or store client secrets and has no connector token,
static-token mode, JWT verifier, OAuth resource-server mode, or stdio path.

## Run

```bash
uv sync
export ODOO_MCP_ODOO_URL=https://odoo.example.com
export ODOO_MCP_HOST=0.0.0.0
export ODOO_MCP_PORT=8000
export ODOO_MCP_TOOL_GROUPS=core,write,collaboration,documents
uv run odoo-agent-mcp
```

Clients connect to `https://mcp.example.com/mcp` with:

```http
Authorization: Bearer <user-created-odoo-mcp-api-key>
```

`/healthz` checks the sidecar process. `/readyz` checks the public Odoo control
API health endpoint without using a user key.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ODOO_MCP_ODOO_URL` | required | Odoo base HTTP(S) URL used for authentication and control API calls |
| `ODOO_MCP_EVENTS_URL` | derived from Odoo URL | Optional explicit WS(S) URL for a separately routed evented/gevent upstream |
| `ODOO_MCP_HOST` | `127.0.0.1` | HTTP bind address |
| `ODOO_MCP_PORT` | `8000` | HTTP bind port |
| `ODOO_MCP_MCP_PATH` | `/mcp` | Streamable HTTP path |
| `ODOO_MCP_TOOL_GROUPS` | `core` | Comma-separated optional tool groups |
| `ODOO_MCP_REQUEST_TIMEOUT_SECONDS` | `30` | Odoo request timeout |
| `ODOO_MCP_VERIFY_TLS` | `true` | Verify Odoo HTTPS and WSS certificates |
| `ODOO_MCP_MAX_RESPONSE_BYTES` | `10485760` | Maximum buffered Odoo response |
| `ODOO_MCP_IDENTITY_CACHE_SECONDS` | `10` | Short successful identity-cache lifetime |
| `ODOO_MCP_USER_LOCK_TIMEOUT_SECONDS` | `5` | Wait limit for another operation by the same user |
| `ODOO_MCP_RETRY_ATTEMPTS` | `3` | Attempts for safe/read-only operations |
| `ODOO_MCP_CIRCUIT_FAILURE_THRESHOLD` | `5` | Transport/gateway failures before opening |
| `ODOO_MCP_CIRCUIT_RESET_SECONDS` | `30` | Delay before one half-open probe |
| `ODOO_MCP_EVENTS_ENABLED` | `true` | Enable Odoo Bus to MCP subscription bridging |
| `ODOO_MCP_EVENT_REFRESH_SECONDS` | `240` | Maximum lifetime of one Odoo WebSocket connection |

When `ODOO_MCP_EVENTS_URL` is unset, the sidecar derives
`wss://<ODOO_MCP_ODOO_URL host>/odoo_mcp/v1/events`. A reverse proxy must send
both `/websocket` and `/odoo_mcp/v1/events` to Odoo's evented/gevent port.
See `nginx.edge.example.conf` for a unified Odoo and MCP edge configuration.

The circuit breaker counts only network failures and HTTP 502/503/504. User
401/403/429 responses, policy errors, validation errors, and oversized
responses never open it.

## Tool Groups

`core` is always enabled. Optional groups are `write`, `collaboration`,
`documents`, `sales`, `accounting`, `inventory`, `projects`, and `hr`.

Enabling a sidecar tool does not grant Odoo access. The user's MCP profile must
independently permit the model, operation, fields, method, companies, and forced
domain.

## Change Workflow

1. Call the matching `odoo_preview_*` tool with an idempotency key.
2. Inspect the exact target count, redacted diff, risk, and expiry.
3. A manager approves the immutable plan in Odoo.
4. Call `odoo_execute_approved_change` with the approval ID.

`odoo://approval/{approval_id}` is a read-only status resource. It is not the
mutation request itself.

## Subscriptions

After bearer validation, the sidecar mints a short-lived, events-only ticket.
It uses that ticket on `/odoo_mcp/v1/events`; the raw API key is not retained by
the watcher. Odoo restricts the WebSocket session to one unguessable channel
for that access assignment. The ticket has an absolute expiry and is never
renewed without another authenticated MCP request. The sidecar also uses a
separate in-process subscription bus per Odoo subject, so one subject's event
cannot be delivered to another subject's listen stream. Notifications contain
only a resource URI and monotonic version; the MCP client must re-read the
resource through normal authorization.

The current FastMCP v4 beta does not yet expose the MCP SDK v2 subscription bus
through its public high-level API. `subscriptions.py` isolates the narrow
adapter that registers `subscriptions/listen`; its HTTP behavior is covered by
an end-to-end test.

## Tests

```bash
uv sync --extra test
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest --cov=odoo_agent_mcp
```

The suite includes real Streamable HTTP MCP sessions for two bearer identities,
preview/approval/execute behavior, access isolation, breaker behavior, and
`subscriptions/listen`. There is intentionally no stdio suite.
