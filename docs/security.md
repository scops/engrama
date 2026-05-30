# Security policy

## Supported versions

Engrama is in active pre-1.0 development. Security fixes ship on `main`
and are released as a new minor version. Older minor versions do not get
backports unless explicitly stated in the release notes.

| Version | Supported          |
| ------- | ------------------ |
| 0.13.x  | :white_check_mark: |
| < 0.13  | :x:                |

## Reporting a vulnerability

**Please do not open a public issue, pull request or discussion for
security vulnerabilities.** That exposes the bug before a fix is ready.

Use GitHub's private vulnerability reporting instead:

1. Open <https://github.com/scops/engrama/security/advisories/new>.
2. File a private advisory with:
   - a short description and impact,
   - steps to reproduce (ideally a minimal script or command),
   - the affected Engrama version, Python version and OS,
   - which backend was active (SQLite or Neo4j),
   - any proof-of-concept payload or sample data you used.

You can expect an acknowledgement within five working days and a status
update within ten. If the report is valid we will agree on a disclosure
timeline before any public release, and credit you in the CHANGELOG if
you want.

## Scope

In scope:

- The `engrama` package and its CLIs (`engrama`, `engrama-mcp`).
- The SQLite and Neo4j storage backends shipped with this repo.
- The MCP adapter, the Python SDK and the embedding-provider layer.
- Default configuration files (`profiles/`, `.env.example`) and the
  build / release pipeline in `.github/workflows/`.

Out of scope (please report upstream):

- Vulnerabilities in third-party services Engrama can talk to — the
  Neo4j server, Ollama, OpenAI, LM Studio, vLLM, llama.cpp, Jina, etc.
- Issues that already require code execution on the host, write access
  to `~/.engrama/`, or compromised API credentials.
- Findings against forks or downstream redistributions; please contact
  those maintainers directly.

## Tenant isolation (multi-tenant)

Since **0.13.0** every node and relation is owned by an `(org_id, user_id)`
identity, and reads are **fail-closed** (Spec 001). This is the isolation
model to understand before exposing Engrama to more than one user.

- **Identity is mandatory on writes.** `engrama_remember` / `engrama_relate`
  stamp `(org_id, user_id)` on the node or edge. A write that cannot resolve
  a complete identity is rejected, never stored unscoped.
- **Reads match nothing without a complete scope.** The scope helpers
  (`scope_filter_cypher` / `scope_filter_sql`) emit `(false)` / `(1 = 0)`
  for a `None`, empty, or half-resolved scope. A read that reaches them
  without a full `(org_id, user_id)` returns **zero rows** — it never widens
  to "see all". There is no see-all admin path through the helpers.
- **Engrama does not authenticate.** It consumes an identity that has
  already been asserted upstream. In a single-process install there is no
  gateway and no headers, so it runs as one stable **standalone identity**
  (derived once at startup) and every read/write shares it — isolation is a
  no-op but the same code path is exercised. In a multi-tenant deployment a
  gateway in front sets `X-Engrama-Org-Id` / `X-Engrama-User-Id` per request;
  exactly one header present resolves to zero results, never an error you can
  pivot on.
- **Defense in depth, three layers:** the per-request resolver at the MCP
  boundary (rejects partial headers), the engine write-guard (raises on a
  direct SDK call without a complete scope), and a CI guard
  (`scripts/check_scoped_queries.py`) that fails the build on any new backend
  query that bypasses the scope helper without an explicit
  `# scope-exempt: <reason>`.
- **Migrating an existing graph.** A pre-0.13 graph has no identity on its
  rows, so under fail-closed reads those rows are invisible. Run
  `engrama migrate tenancy --dry-run` to preview, then
  `engrama migrate tenancy --owner-sub <sub> --apply` to stamp ownership and
  restore visibility.

### Admin / cross-tenant tools

Two tools are **not** isolated per tenant by design and a multi-tenant
gateway should gate them so a normal tenant cannot reach them:

- `engrama_status` — runtime introspection; its counts are **deployment-wide**
  and it requires no identity.
- `engrama_reindex` — its candidate scan is scoped to the calling tenant
  (it leaks no cross-tenant data), but it is an admin-flavoured bulk
  re-embed; a gateway may still gate it for cost/abuse.

`engrama_status` lists both in an `admin_tools` field of its own response, so
a gateway can discover what to gate at runtime instead of hardcoding names.
Engrama OSS only **declares** this boundary; enforcing it (and all
authentication) is the gateway's job.

## Hardening notes for operators

A few defaults worth knowing when deploying Engrama:

- `~/.engrama/engrama.db` is plain SQLite. Treat it like any other
  application database: keep it off shared filesystems, back it up,
  and rely on filesystem permissions for at-rest protection.
- Embedding providers reached via `OPENAI_BASE_URL` should use HTTPS
  unless the endpoint is on localhost or a trusted network.
- The MCP adapter is intended to be talked to by a local client (Claude
  Desktop, an SDK, etc.). It is not hardened for direct exposure on the
  public internet — put it behind your own authenticated gateway if you
  need remote access.
- The optional **Streamable HTTP transport** (`ENGRAMA_TRANSPORT=http`)
  ships **without authentication**. Bound to its default loopback address
  (`127.0.0.1`) it has the same attack surface as stdio — only local
  processes can reach it. Binding it off-loopback (`0.0.0.0`, a LAN IP, a
  reverse proxy or a tunnel) turns it into an unauthenticated read/write
  endpoint for the whole memory graph: don't, until OAuth lands. The
  built-in `Origin`/`Host` validation guards against DNS-rebinding from a
  local browser. See the [Streamable HTTP guide](saas/streamable-http.md).
