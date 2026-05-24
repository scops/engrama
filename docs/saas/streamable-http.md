# Streamable HTTP transport

Engrama's MCP server speaks two transports:

| Transport | When | How it's selected |
|-----------|------|-------------------|
| **stdio** (default) | Local desktop clients that launch the server as a subprocess (Claude Desktop's standard config). | `ENGRAMA_TRANSPORT=stdio` (or unset). |
| **Streamable HTTP** | Running Engrama as a long-lived local HTTP server you connect to over the network. | `ENGRAMA_TRANSPORT=http`. |

The HTTP transport is built on the MCP SDK's bundled FastMCP
(`mcp.server.fastmcp`) — no extra dependency. The default stays `stdio`
so existing Claude Desktop setups are untouched.

!!! warning "No authentication in this phase"
    The HTTP transport ships **without auth**. It binds to loopback
    (`127.0.0.1`) by default and rejects cross-origin requests, but it
    does **not** verify any token. Do not expose it on a public
    interface. OAuth 2.1 against an external issuer is a later phase;
    the `/.well-known/oauth-protected-resource` endpoint below is the
    hook for it.

## Configuration

All HTTP settings are environment variables (CLI flags override them):

| Env var | CLI flag | Default | Purpose |
|---------|----------|---------|---------|
| `ENGRAMA_TRANSPORT` | `--transport` | `stdio` | `stdio` or `http`. |
| `ENGRAMA_HTTP_HOST` | `--host` | `127.0.0.1` | Bind address (HTTP mode). |
| `ENGRAMA_HTTP_PORT` | `--port` | `8000` | TCP port (HTTP mode). |
| `ENGRAMA_ALLOWED_ORIGINS` | `--allowed-origins` | loopback only | CSV of allowed `Origin` headers. |
| `ENGRAMA_AUTH_ISSUER` | `--auth-issuer` | _(unset)_ | OAuth issuer for the RFC 9728 stub. Unset → endpoint 404s. |

The MCP endpoint is served at **`/mcp`**.

## Starting in HTTP mode (local)

=== "PowerShell"

    ```powershell
    $env:ENGRAMA_TRANSPORT = "http"
    engrama-mcp
    # or, equivalently:
    engrama-mcp --transport http --host 127.0.0.1 --port 8000
    ```

=== "bash"

    ```bash
    ENGRAMA_TRANSPORT=http engrama-mcp
    # or:
    engrama-mcp --transport http --host 127.0.0.1 --port 8000
    ```

The backend selection is unchanged from stdio mode (`--backend sqlite`
default, `--backend neo4j` plus the `NEO4J_*` vars to opt in).

## Endpoints

| Path | Method | Purpose |
|------|--------|---------|
| `/mcp` | POST/GET | The MCP Streamable HTTP endpoint. |
| `/health` | GET | Liveness/readiness probe — 200 if the backend answers, 503 otherwise. |
| `/.well-known/oauth-protected-resource` | GET | RFC 9728 metadata stub (404 until `ENGRAMA_AUTH_ISSUER` is set). |

### `/health`

Returns `200` with `{"status": "ok", "backend": ..., "node_count": ...}`
when the configured backend responds, `503` with
`{"status": "error", ...}` when it does not. Useful for Kubernetes
liveness/readiness probes in a future deployment phase.

```bash
curl -i http://127.0.0.1:8000/health
```

It is intentionally **not** guarded by the Origin check (probes send no
`Origin` header). It owns a small cached connection of its own — see
[Stateless mode](#why-stateless) for why custom routes can't reuse the
per-request store.

### `/.well-known/oauth-protected-resource`

A stub for the upcoming OAuth phase:

```bash
# No issuer configured → 404
curl -i http://127.0.0.1:8000/.well-known/oauth-protected-resource

# With an issuer → RFC 9728 document
ENGRAMA_AUTH_ISSUER=https://auth.example.com engrama-mcp --transport http
curl -s http://127.0.0.1:8000/.well-known/oauth-protected-resource
# {"resource": "http://127.0.0.1:8000/mcp",
#  "authorization_servers": ["https://auth.example.com"]}
```

The next phase only has to set `ENGRAMA_AUTH_ISSUER` (and wire a token
verifier) — no change to this endpoint's code.

## Origin validation (anti DNS-rebinding)

The HTTP transport uses the MCP SDK's built-in DNS-rebinding protection.
On every request to `/mcp` it validates:

- **`Origin`** against `ENGRAMA_ALLOWED_ORIGINS` — a disallowed Origin is
  rejected with **403**. A missing `Origin` (same-origin / non-browser
  client like `curl`) is allowed.
- **`Host`** against the loopback allow-list derived from `--host`/`--port`
  — a mismatched Host is rejected with **421**.

The default Origin allow-list is loopback only, including port wildcards
so browser-style clients connecting to `http://localhost:8000` work
without extra configuration:

```
http://localhost, http://127.0.0.1, http://localhost:*, http://127.0.0.1:*
```

Override it for a specific client:

```bash
ENGRAMA_ALLOWED_ORIGINS="http://localhost:8000,https://my-client.example" \
  engrama-mcp --transport http
```

Quick check:

```bash
# Disallowed Origin → 403
curl -i -H "Origin: http://evil.com" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST http://127.0.0.1:8000/mcp

# No Origin (curl) → passes the security check
curl -i -H "Accept: application/json, text/event-stream" \
  http://127.0.0.1:8000/mcp
```

## Why stateless { #why-stateless }

The server runs with `stateless_http=True`. Engrama's MCP tools are
plain request/response calls — none of them use MCP **sampling** or
**elicitation**, which are the features that require a sticky session
between client and server. Running stateless keeps the server simple and
horizontally scalable (any instance can serve any request).

The trade-off: in stateless mode the SDK creates a fresh transport for
each request and re-enters the server lifespan on every MCP call, so the
graph store is opened and closed per request. For SQLite (the default)
this is cheap; for Neo4j it means a driver handshake per call. This is
acceptable for local/single-user use; a future deployment phase can
revisit connection pooling if it becomes a bottleneck.

A direct consequence: **custom routes (`/health`) never see the
per-request lifespan context**, which is why `/health` maintains its own
lazily-created, cached backend connection rather than reaching into the
MCP request state.

## Connecting clients

### `mcp` CLI / Inspector (manual testing)

The MCP Inspector or any MCP HTTP client points at the `/mcp` URL:

```bash
npx @modelcontextprotocol/inspector
# Transport: "Streamable HTTP"
# URL: http://127.0.0.1:8000/mcp
```

From the Inspector you can list tools (`engrama_status`, `engrama_search`,
…) and call them to confirm the server responds end to end.

### Claude Desktop (custom integration)

Some Claude Desktop builds accept a custom HTTP MCP server; others
restrict custom integrations to HTTPS. To try it:

1. Start Engrama in HTTP mode (above).
2. In Claude Desktop, add a custom MCP server / integration pointing at
   `http://localhost:8000/mcp`.
3. Confirm Engrama's tools appear and `engrama_status` returns the
   expected backend/vault.

**Known limitations (this phase):**

- **No auth.** Claude Desktop may warn about or refuse an unauthenticated
  custom integration.
- **HTTPS requirement.** If your build requires HTTPS for custom
  integrations, front the server with a locally-trusted TLS cert
  ([`mkcert`](https://github.com/FiloSottile/mkcert)) and point the
  client at the `https://` URL — or defer Claude Desktop integration to
  the OAuth/TLS phase and validate with the MCP Inspector for now.

The goal of this phase is that **the server responds correctly over
HTTP**; full Claude Desktop acceptance may have to wait for the auth
phase depending on your build.

## Operational differences vs stdio

| | stdio | Streamable HTTP |
|---|-------|-----------------|
| Process model | Launched as a subprocess by the client. | Long-lived server you start and connect to. |
| Lifecycle | One process per client session. | One process, many requests. |
| Store lifespan | Opened once, reused for the session. | Re-opened per request (stateless). |
| Network exposure | None (pipes). | Binds a TCP port; Origin/Host validated. |
| Health probe | N/A. | `GET /health`. |
| Auth | N/A (local trust). | None yet — loopback + Origin check only. |
