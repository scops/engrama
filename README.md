# Engrama

> Graph-based long-term memory framework for AI agents.

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![Backend](https://img.shields.io/badge/backend-SQLite_%7C_Neo4j-green.svg)](docs/backends.md)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha%20%C2%B7%20install%20from%20source-orange.svg)](#quick-start-sqlite-zero-dep)

Engrama gives any AI agent persistent, structured memory backed by a
**knowledge graph**. Instead of flat key-value stores or opaque vector
databases, Engrama stores **entities**, **observations**, and
**relationships** — and lets agents traverse that graph to reason about
their accumulated knowledge.

Two backends are first-class:

- **SQLite + `sqlite-vec`** (default since 0.9) — single file, zero
  external services, `git clone` + `uv sync` and you're running
  (Engrama is not yet on PyPI; install from source).
- **Neo4j 5.26 LTS** (opt-in) — for multi-process production setups,
  large-scale vector search, or teams that already use Cypher.

The data model is identical on both. See **[docs/backends.md](docs/backends.md)**
for a full decision guide; the rest of this README assumes the SQLite
default.

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

### Step 1: Clone and install

```bash
git clone https://github.com/scops/engrama
cd engrama
uv sync
```

### Step 2: Initialise the schema

```bash
uv run engrama init --profile developer
```

### Step 3: Verify

```bash
uv run engrama verify
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
uv run engrama search "FastAPI"
uv run engrama reflect
```

---

## Quick start (Neo4j, opt-in)

If you need multi-process writes, very large vector indexes, or an existing Cypher toolchain, install with the Neo4j extra:

```bash
git clone https://github.com/scops/engrama
cd engrama
uv sync --extra neo4j
```

Configure your credentials by copying `.env.example` to `.env` and setting `GRAPH_BACKEND=neo4j`. Start Neo4j with `docker compose up -d`, and then initialize the schema:

```bash
uv run engrama init --profile developer
uv run engrama verify
```

---

## 📚 Full Documentation

All further details, including **MCP integration (Claude Desktop)**, **Obsidian sync**, **Architecture**, and the complete **API Reference**, are available in the official documentation.

👉 **[Read the Full Documentation](https://scops.github.io/engrama/)**
