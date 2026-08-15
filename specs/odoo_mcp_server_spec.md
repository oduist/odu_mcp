# Odoo Agent MCP Sidecar Specification

## Scope And Deployment

The sidecar is an HTTP-only MCP server built on exact FastMCP `4.0.0b3` and MCP
SDK v2. It runs as exactly one replica. Distributed state, Redis, sticky
sessions, and distributed locks are outside scope.

The sidecar owns protocol concerns and never accesses the Odoo database or ORM.
Odoo remains authoritative for identity, authorization, profiles, approvals,
execution, and audit.

## Transport

- Streamable HTTP is served at `/mcp`.
- The server runs with stateless HTTP and JSON responses where allowed by MCP.
- `subscriptions/listen` remains an SSE streaming response as required by the
  2026-07-28 protocol.
- `/healthz` is local liveness.
- `/readyz` calls Odoo's public health route.

## Authentication And Identity

The MCP bearer token is a user-created, `mcp`-scoped Odoo API key.

1. The verifier calls `GET /odoo_mcp/v1/identity` with that bearer.
2. Odoo returns the effective database, user, and profile.
3. The verifier creates a FastMCP access token whose subject is
   `odoo:<database>:<user_id>`.
4. Successful identities may be cached briefly by SHA-256 token digest.
5. Each tool/resource call forwards the current raw bearer only in that
   request's Odoo `Authorization` header.

The shared Odoo `httpx.AsyncClient` has no authorization header. The raw key is
not logged or placed in global state.

## Tool Contract

Core tools expose server identity, current identity, model discovery, schema,
search, read, count, aggregate, approval status, and approved execution.

Optional groups expose preview tools for writes/collaboration/documents and
bounded read-only summaries for sales, accounting, inventory, projects, and HR.
There is no generic ORM tool, arbitrary method tool, force parameter, or caller
supplied Odoo user/profile.

Resources:

- `odoo://server/capabilities`
- `odoo://model/{model}/schema`
- `odoo://record/{model}/{record_id}`
- `odoo://approval/{approval_id}`

Prompts teach evidence-based reads and the preview/approve/execute workflow.

## Concurrency

The instructions tell clients not to invoke tools concurrently for the same
Odoo identity, but correctness does not rely on that instruction. The runtime
uses a per-subject semaphore with a bounded wait. Different users can run in
parallel; operations for one user are serialized.

## Retry And Circuit Breaker

Only read-only/idempotent operations are retried. Mutations are attempted once.

The global Odoo transport breaker has closed, open, and half-open states:

- only network errors and HTTP 502/503/504 increment failures;
- HTTP 4xx, quota/policy errors, invalid inputs, and response-size errors do not
  increment the breaker;
- after the threshold it opens for the configured reset interval;
- after the interval exactly one half-open probe is admitted;
- probe success closes the breaker; probe failure reopens it.

Generation tracking prevents old in-flight requests from incorrectly closing a
newly opened circuit.

## Subscriptions

After successful identity verification, an in-process watcher is ensured for
the subject:

1. It uses the raw key once to mint an Odoo event ticket.
2. It clears its raw-key reference.
3. It opens Odoo's event WebSocket with the event-only ticket.
4. It bounds every WebSocket connection by the configured refresh interval.
5. It stops when the ticket reaches its absolute expiry; a later authenticated
   MCP request may create a watcher with a new ticket.
6. It validates and deduplicates `(subject, URI, version)` events.
7. It publishes a standard MCP `ResourceUpdated` event only on that subject's
   in-process subscription bus.

FastMCP v4 beta does not currently expose the MCP SDK v2 subscription bus via a
public high-level method. One isolated adapter installs SDK `ListenHandler` on
the FastMCP low-level server and selects an SDK in-memory bus by authenticated
Odoo subject. This adapter must remain covered by an HTTP end-to-end isolation
test and should be removed when FastMCP provides a public equivalent.

MCP notifications contain only the resource URI. The version is used internally
for deduplication; clients re-read the resource for current authorized state.

## Configuration

All variables use the `ODOO_MCP_` prefix. Required: `ODOO_URL`. Optional:
`HOST`, `PORT`, `MCP_PATH`, request and size limits, tool groups,
logging, identity-cache TTL, per-user wait timeout, retry count, breaker
threshold/reset, and event enable/refresh settings.

There are no transport-selection, connector-secret, static-token, or JWT/OAuth
resource-server settings.

## Verification

The automated suite must cover:

- Odoo identity verification, invalid keys, and cache behavior;
- bearer forwarding without cross-user leakage;
- per-user serialization and cross-user parallelism;
- safe retries and non-retried mutations;
- breaker exclusions, opening, and one half-open probe;
- component registration and health/readiness routes;
- real HTTP MCP sessions for two users;
- preview, cross-user denial, approval, and execute flow;
- Odoo event parsing, version deduplication, and event-ticket minting;
- real HTTP `subscriptions/listen` delivery.

No stdio test or implementation is permitted.
