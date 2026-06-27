# Engrama

> Graph-based long-term memory framework for AI agents.

[![PyPI](https://img.shields.io/pypi/v/engrama.svg)](https://pypi.org/project/engrama/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![Backend](https://img.shields.io/badge/backend-SQLite_%7C_Neo4j-green.svg)](docs/backends.md)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#quick-start-sqlite-zero-dep)

Engrama gives any AI agent persistent, structured memory backed by a
**knowledge graph**. Instead of flat key-value stores or opaque vector
databases, Engrama stores **entities**, **observations**, and
**relationships** — and lets agents traverse that graph to reason about
their accumulated knowledge.

Two backends are first-class:

- **SQLite + `sqlite-vec`** (default since 0.9) — single file, zero
  external services, `pip install engrama` and you're running.
- **Neo4j 5.26 LTS** (opt-in) — for multi-process production setups,
  large-scale vector search, or teams that already use Cypher.

The data model is identical on both. See **[docs/backends.md](docs/backends.md)**
for a full decision guide; the rest of this README assumes the SQLite
default.

Since **0.13.0**, every node and relation is owned by an
`(org_id, user_id)` identity and reads are **fail-closed**: a missing or
partial scope matches nothing rather than falling back to "see all". A
single-process install runs as one stable standalone identity and needs
no configuration; a multi-tenant deployment supplies the identity per
request from an authenticating gateway. Each identity can permanently
erase its own memory through the `engrama_gdpr_forget` tool
(GDPR right-to-erasure). See
**[docs/security.md](docs/security.md#tenant-isolation-multi-tenant)**.

Inspired by Karpathy's second-brain concept, but built for agents
instead of humans — and with graphs instead of wikis.

---

## Why graphs?

| | Flat JSON / KV | Vector DB | **Engrama (Graph)** |
|---|---|---|---|
| Relationship queries | ❌ | ❌ | ✅ native |
| Scales to 10k+ memories | ❌ slow | ✅ | ✅ |
| Works without embeddings | ✅ | ❌ | ✅ (optional) |
| Local-first / private | ✅ | depends | ✅ |
| Zero external services | ✅ | ❌ | ✅ (SQLite) |
| "What projects use FastMCP?" | full scan | approximate | 1-hop traversal |

---

## Prerequisites

You need two things to run on the default SQLite backend. **Docker is
not required** unless you opt into Neo4j.

| Requirement | Version | How to check | Install guide |
|---|---|---|---|
| **Python** | 3.11 or newer | `python --version` | [python.org/downloads](https://www.python.org/downloads/) |
| **uv** (Python package manager) | any recent | `uv --version` | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |

> **Windows users:** after installing Python, make sure "Add Python to
> PATH" is checked. After installing uv, you may need to restart your
> terminal.

**Optional:**

- [Obsidian](https://obsidian.md/) — for vault sync features.
- A local embedder for semantic search.
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) —
  only if you opt into the Neo4j backend.

---

## Quick start (SQLite, zero-dep)

### Step 1: Install

From PyPI (recommended):

```bash
pip install engrama          # or: uv add engrama
```

Or from source, for development:

```bash
git clone https://github.com/scops/engrama
cd engrama
uv sync
```

> The commands below assume a PyPI install (`engrama ...`). From a source
> checkout, prefix each one with `uv run` (`uv run engrama ...`).

### Step 2: Initialise the schema

```bash
engrama init --profile developer
```

### Step 3: Verify

```bash
engrama verify
```

### Step 4: Use it

**A) From Python:**
```python
from engrama import Engrama

with Engrama() as eng:
    eng.remember("Technology", "FastAPI", "High-performance async framework")
    eng.associate("MyProject", "Project", "USES", "FastAPI", "Technology")
    results = eng.search("microservices")
```

**B) From the command line:**
```bash
engrama search "FastAPI"
engrama reflect
```

---

## Quick start (Neo4j, opt-in)

If you need multi-process writes, very large vector indexes, or an existing Cypher toolchain, install with the Neo4j extra:

```bash
pip install "engrama[neo4j]"     # or, from source: uv sync --extra neo4j
```

Configure your credentials by copying `.env.example` to `.env` and setting `GRAPH_BACKEND=neo4j`. Start Neo4j with `docker compose up -d`, and then initialize the schema:

```bash
engrama init --profile developer
engrama verify
```

---

## Security considerations

Engrama stores everything an agent learns, so treat the memory graph as
sensitive data. The full policy lives in
**[docs/security.md](docs/security.md)**; the essentials:

- **Data residency.** On the default SQLite backend all data lives in a
  single local file (`~/.engrama/engrama.db` by default) — nothing leaves
  your machine. It is plain SQLite: keep it off shared filesystems, back
  it up, and rely on filesystem permissions for at-rest protection. On the
  Neo4j backend, data resides wherever you host Neo4j; you own that
  deployment and its region.
- **Neo4j authentication.** Supply credentials through `.env` /
  environment variables (`NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`)
  — never commit them. The shipped `docker-compose.yml` is for local dev;
  change the default password and enable TLS before any networked use.
- **Embedding providers.** Endpoints reached via `OPENAI_BASE_URL` should
  use HTTPS unless they are on localhost or a trusted network. With
  `EMBEDDING_PROVIDER=null` no text is sent anywhere; search degrades to
  fulltext-only.
- **Tenant isolation.** Since 0.13.0 every node and relation is owned by an
  `(org_id, user_id)` identity and reads are **fail-closed**. A single
  install runs as one stable standalone identity; a multi-tenant
  deployment must inject the identity per request from an authenticating
  gateway (set `ENGRAMA_REQUIRE_IDENTITY=1` to fail closed on missing
  headers). See
  **[docs/security.md](docs/security.md#tenant-isolation-multi-tenant)**.
- **Right to erasure.** Each identity can permanently erase its own memory
  via the `engrama_gdpr_forget` tool (GDPR). There is no undo and no
  server-side backup.
- **Network exposure.** The MCP server is meant for a local client. The
  optional Streamable HTTP transport ships **without authentication** —
  keep it on loopback or behind your own authenticated gateway.

---

## 📚 Full Documentation

All further details, including **MCP integration (Claude Desktop)**, **Obsidian sync**, **Architecture**, and the complete **API Reference**, are available in the official documentation.

👉 **[Read the Full Documentation](https://scops.github.io/engrama/)**
