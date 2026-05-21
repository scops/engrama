# Security policy

## Supported versions

Engrama is in active pre-1.0 development. Security fixes ship on `main`
and are released as a new minor version. Older minor versions do not get
backports unless explicitly stated in the release notes.

| Version | Supported          |
| ------- | ------------------ |
| 0.9.x   | :white_check_mark: |
| < 0.9   | :x:                |

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
