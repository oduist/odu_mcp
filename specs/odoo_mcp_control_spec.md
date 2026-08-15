# Odoo MCP Control Plane Specification

## Scope

`odoo_mcp_control` is the authoritative security and execution layer for MCP
access to Odoo 19. It does not implement the MCP wire protocol. It exposes a
small HTTP API consumed by one FastMCP sidecar.

This release supports fresh databases only. Installation or upgrade must fail
if the obsolete credential table or model metadata is present. No migration,
compatibility alias, or legacy-secret path is allowed.

## Identity And API Keys

- Users create API keys through the standard Odoo profile UI.
- The wizard defaults to **MCP only**, which stores scope `mcp`.
- An `mcp` key authenticates only routes using `auth="mcp"`; it cannot satisfy
  Odoo's normal `rpc` scope.
- API-key expiration and active-user checks remain Odoo core behavior.
- The bearer owner becomes `request.env.user`; callers cannot provide or select
  another user ID.

## `odoo.mcp.access`

Each Odoo user has at most one MCP access assignment:

- `name`, `active`
- `user_id` (unique)
- `profile_id`
- usage counters and last-use timestamp
- an unguessable event channel and monotonic event version

An absent, inactive, or profile-disabled assignment returns HTTP 403. Access
assignments do not create, display, or store API-key material.

## Profiles And Policy

The existing profile and policy models remain responsible for:

- allowed companies;
- allowed models and operations;
- readable and writable fields;
- forced domains;
- record and batch limits;
- allowlisted public methods and argument bounds;
- attachment/report/schema/aggregation capabilities;
- per-minute and daily quotas;
- approval TTL and low-risk auto-approval policy.

Every business ORM operation runs as `access.user_id`, without superuser mode,
with the profile's allowed company context. Odoo ACLs and record rules apply in
addition to MCP policy. Forced-domain postconditions are checked after creates
and updates and roll back changes that escape policy.

## HTTP API

### Public

- `GET /odoo_mcp/v1/health`

### MCP API-key authenticated

- `GET /odoo_mcp/v1/identity`
- `GET /odoo_mcp/v1/capabilities`
- `POST /odoo_mcp/v1/execute`
- `POST /odoo_mcp/v1/events/ticket`

`/identity` returns the database, user ID, login, name, and profile code. It is
the sidecar's token-verification endpoint.

`/execute` accepts:

```json
{
  "operation": "records.search",
  "params": {}
}
```

Responses use a common envelope:

```json
{
  "ok": true,
  "request_id": "uuid",
  "data": {}
}
```

Errors include a stable code, safe message, and retryable flag. Request and
response sizes are bounded. Correlation IDs must be UUIDs; invalid inbound IDs
are replaced.

## Read Operations

The service supports bounded capabilities, identity, model listing and schema,
search/read/count/aggregation, attachment reads, report rendering, and approval
status. Requested domains are always ANDed with the forced domain. Requested
fields must be an explicit subset of policy-visible fields.

## Change Workflow

Mutations follow:

```text
preview -> immutable approval -> manager decision -> explicit execute
```

- Preview normalizes and validates the exact action and creates an idempotent
  approval record.
- Approval records are immutable outside system transitions.
- Approval is performed by an MCP manager in Odoo.
- Execute accepts only an approval ID, locks the row, verifies payload hash and
  expiry, and stores the result.
- Re-executing a completed approval returns the stored result without repeating
  the mutation.
- `odoo://approval/{approval_id}` represents read-only status, not a mutation.

Supported preview actions are create, update, delete, allowlisted method call,
message post, activity scheduling, and attachment creation.

## Audit

Every operation writes an immutable audit row linked to `access_id`, profile,
user, request ID, operation, model, outcome, duration, status, error code, and
approval where applicable. Inputs are hashed and summarized with sensitive
fields redacted. Raw API keys and binary bodies are never recorded.

## Events

`POST /events/ticket` returns a random events-only bearer ticket. Only its
SHA-256 digest is stored. Its short lifetime is absolute; WebSocket handshakes
record usage but do not extend expiry.

`GET /odoo_mcp/v1/events` is an Odoo Bus WebSocket route using
`auth="mcp_event"`. Ticket authentication creates a public-only WebSocket
session whose context is restricted to the access assignment's unguessable bus
channel. The standard broadcast, group, partner, and caller-provided channels
are not added for this session.

Approval state changes and MCP-executed record changes publish:

```json
{
  "type": "resource.updated",
  "uri": "odoo://approval/<uuid>",
  "version": 7
}
```

No business fields are sent. Consumers re-read the resource through normal MCP
and Odoo authorization. Bus messages are emitted at transaction commit.

## Security Acceptance Criteria

- An MCP key cannot authenticate normal Odoo RPC.
- A user cannot select another user, access assignment, or profile.
- Odoo ACLs, record rules, company rules, and MCP policy all apply.
- Missing access is denied even when the API key itself is valid.
- Approval payloads cannot be changed after preview.
- Audit records cannot be edited or manually deleted.
- Event tickets cannot call business APIs and can subscribe only to their one
  assigned event channel.
- The obsolete schema causes an explicit installation/upgrade failure.
