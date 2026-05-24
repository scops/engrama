# Streamable HTTP transport

Engrama's MCP server speaks two transports:

| Transport | When | How it's selected |
|-----------|------|-------------------|
| **stdio** (default) | Local desktop clients that launch the server as a subprocess (Claude Desktop's standard config). | `ENGRAMA_TRANSPORT=stdio` (or unset). |
| **Streamable HTTP** | Running Engrama as a long-lived local HTTP server you connect to over the network. | `ENGRAMA_TRANSPORT=http`. |

The HTTP transport is built on the MCP SDK's bundled FastMCP
(`mcp.server.fastmcp`) — no extra dependency. The default stays `stdio`
so existing Claude Desktop setups are untouched.

!!! danger "Bind to loopback only — there is no authentication yet"
    The HTTP transport ships **without auth**. Run it bound to
    `127.0.0.1` (the default) and **never** expose it on a public or
    LAN-reachable interface until the OAuth phase lands. See
    [Security model](#security-model).

## Security model { #security-model }

**Local HTTP on loopback has the same attack surface as stdio.** With the
default bind (`127.0.0.1`), the only processes that can reach `/mcp` are
those already running on your machine — exactly the trust boundary stdio
relies on (a local client launching and talking to a local server).
Switching a local Claude Desktop / SDK client from stdio to loopback HTTP
does **not** widen your exposure.

The surface only grows if **you** change the deployment:

- **Binding off-loopback** — `ENGRAMA_HTTP_HOST=0.0.0.0` or a LAN IP, a
  reverse proxy, or an SSH / `ngrok`-style tunnel — turns the server into
  an **unauthenticated remote endpoint**. Anyone who can reach the port
  can read and write the entire memory graph. Don't, not in this phase.
- **A malicious local web page** could try to script a browser into
  POSTing to `http://127.0.0.1:8000/mcp` (a DNS-rebinding / CSRF-style
  attack). The built-in [Origin/Host validation](#origin-validation) is
  the guard: cross-origin requests are rejected with 403, and only
  loopback `Host` values are accepted.

Rules of thumb for this phase:

- ✅ Loopback bind + local client → same trust as stdio. Fine.
- ✅ Keep the default `ENGRAMA_ALLOWED_ORIGINS` (loopback only).
- ❌ No off-loopback bind, no public / LAN exposure, no tunnel — unless
  you put your own authenticated gateway **and** TLS in front.
- ⏭ Real authentication (OAuth 2.1) is the next phase; the
  `/.well-known/oauth-protected-resource` stub is its hook.

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
[Session mode](#session-mode) for why custom routes can't reuse the
MCP session store.

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

## Origin validation (anti DNS-rebinding) { #origin-validation }

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

## Session mode (stateful) { #session-mode }

The server runs **stateful** (`stateless_http=False`, the SDK default).
On `initialize` the server returns an `Mcp-Session-Id` header; the client
reuses it on every following POST, and the server lifespan — opening the
graph store, vault and embedder — runs **once per session** rather than
once per request.

This is required by conversational MCP clients (claude.ai, Claude
Desktop). Under `stateless_http=True` the SDK assigns no session id and
re-enters the lifespan on every POST (re-initialising Neo4j/Ollama/vault
each time); those clients see the session die after each request and
**fail to register the tools**. Stateless is only worthwhile for
horizontally-scaled, fan-out deployments backed by a shared event store —
not the local/single-server case here. Engrama's tools are plain
request/response calls (no MCP **sampling** or **elicitation**), so a
sticky session costs nothing functionally.

A consequence of the SDK's design: **custom routes (`/health`) never see
the MCP session lifespan context** (it belongs to the MCP server, not the
ASGI app), which is why `/health` maintains its own lazily-created, cached
backend connection rather than reaching into the MCP request state.

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
| Store lifespan | Opened once, reused for the session. | Opened once per session (stateful). |
| Network exposure | None (pipes). | Binds a TCP port; Origin/Host validated. |
| Health probe | N/A. | `GET /health`. |
| Auth | N/A (local trust). | None yet — loopback + Origin check only. |
