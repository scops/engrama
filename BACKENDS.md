# Choosing a backend

> Engrama runs on two interchangeable storage backends. This guide tells you
> which one to pick and why.

Engrama 0.9 introduces **portable storage**: SQLite + the `sqlite-vec`
extension, both bundled into a single file under `~/.engrama/engrama.db`.
The original Neo4j backend is still fully supported — it's now an opt-in
extra.

If you don't know which one to pick, **start with SQLite**. You can switch
later with one environment variable.

---

## At a glance

| | **SQLite (default)** | **Neo4j (opt-in)** |
|---|---|---|
| Install | `git clone` + `uv sync` | `git clone` + `uv sync --extra neo4j` + Docker |
| External services | none | Neo4j 5.26 LTS in Docker |
| First run | seconds | ~15s for the database to boot |
| Disk footprint | one `.db` file | Neo4j data directory + Docker image (~500 MB) |
| Portability | copy the `.db` file anywhere | dump/restore via `neo4j-admin` |
| Concurrency | one writer, many readers (WAL) | many readers and writers |
| Vector search | `sqlite-vec` (brute-force, fine to ~100k vectors) | Neo4j vector index (HNSW, scales further) |
| Multi-process write access | not recommended | yes |
| Cloud / remote access | local file only | bolt://host:7687 |
| Cypher query language | not available | yes |
| Memory profile | tiny (single SQLite process) | JVM heap (~1 GB minimum) |
| Operates without Docker | ✅ | ❌ |

The data model — labels, relationships, faceted classification — is
**identical** on both backends. Anything you can store on SQLite you can
later move to Neo4j (and vice versa) without restructuring your graph.

---

## The decision tree

```
Are you running Engrama on a single laptop / VM / container, for one user?
├─ Yes → SQLite. You're done.
│
└─ No → Multiple processes need to write at the same time?
        ├─ Yes → Neo4j.
        │
        └─ No → Do you need >100k embeddings, or expect to in <12 months?
                ├─ Yes → Neo4j.
                │
                └─ No → Do you need ad-hoc Cypher analytics or a graph viewer?
                        ├─ Yes → Neo4j.
                        │
                        └─ No → SQLite.
```

In practice the first branch covers ~90% of users.

---

## When to pick SQLite

- **You're getting started.** Zero install friction, no Docker, no JVM.
  `git clone … && uv sync && uv run engrama init` and you're querying
  the graph (Engrama is not yet on PyPI; install from source for now).
- **Single-agent setups.** One Claude Desktop, one MCP client, one
  long-running script. SQLite handles this perfectly.
- **CI runs and tests.** No external service to spin up — `pytest` works
  out of the box on a fresh checkout.
- **Embedded distribution.** Shipping a tool that includes Engrama as a
  library? Your users get a working memory layer with no Docker prereqs.
- **Edge / resource-constrained hosts.** No JVM means Engrama runs
  comfortably on a Raspberry Pi or a 512 MB VM.
- **Portable research notebooks.** Send a colleague your `.db` file and
  they have your full graph — no schema migration needed.

---

## When to pick Neo4j

- **Production multi-user setups.** Multiple agents (or humans via Bloom
  / Browser) writing concurrently to the same graph.
- **Large-scale vector search.** `sqlite-vec` does brute-force similarity;
  fine up to ~100k vectors but Neo4j's HNSW index will outperform it
  beyond that.
- **You already have Cypher pipelines.** If your team writes Cypher for
  analytics, business logic, or migrations, keep that investment.
- **You need Bloom / Neo4j Browser for visual exploration.** SQLite has
  no equivalent native UI.
- **Cluster / high availability** (Neo4j Enterprise). SQLite is a
  single-file database — no replication.

---

## Switching from one to the other

Swap one environment variable. The data model is identical, so any tool
or skill that works on one backend works on the other.

### From SQLite to Neo4j

```bash
# 1. Install the extra and start Neo4j
uv sync --extra neo4j
docker compose up -d                 # uses docker-compose.yml from the repo

# 2. Tell Engrama to use it
echo 'GRAPH_BACKEND=neo4j' >> .env
echo 'NEO4J_PASSWORD=...' >> .env

# 3. (Optional) re-create the vector index for hybrid search
uv run engrama init --profile developer
uv run engrama reindex
```

If `GRAPH_BACKEND=neo4j` is set but the Python extra is missing, both
the CLI and the MCP server now fail with an explicit install hint
instead of a generic import or startup error.

To carry data across, the simplest path today is: configure a temporary
SDK script that reads from SQLite and writes to Neo4j via two `Engrama`
contexts. A first-class export tool is on the roadmap.

### From Neo4j to SQLite

```bash
# 1. Tell Engrama to use SQLite
echo 'GRAPH_BACKEND=sqlite' >> .env
echo 'ENGRAMA_DB_PATH=~/.engrama/engrama.db' >> .env  # optional, this is the default

# 2. Sync from your Obsidian vault — vault is portable by design (DDR-002)
uv run engrama-mcp     # or use Claude Desktop
# then: engrama_sync_vault
```

Because relations are persisted in vault frontmatter (DDR-002), the
Obsidian vault is itself a portable backup of the full graph. Pointing a
fresh SQLite install at the same vault and running `engrama_sync_vault`
rebuilds the graph from scratch.

---

## How does this work under the hood?

Both backends implement the same `GraphStore`, `VectorStore`, and
`EmbeddingProvider` protocols (`engrama/core/protocols.py`). A single
factory in `engrama/backends/__init__.py` reads `GRAPH_BACKEND` from the
environment and returns the right implementation.

Skills, the MCP server, the CLI, and the Python SDK are written against
the protocols — they don't know which backend is underneath. That's why
swapping is a one-variable change.

See [ARCHITECTURE.md](ARCHITECTURE.md#protocol-layer-and-backends) for
the full layer diagram, and [DDR-004](DDR-004.md) for the design
rationale of the portable backend.

---

## Frequently asked

**Can I run both backends at the same time?**
You can, but a single Engrama process binds to one. Different processes
can target different backends — useful for testing or migrations.

**Does SQLite support all the features Neo4j has?**
For the public Engrama API (the 12 MCP tools, the SDK, the CLI), yes —
they're feature-equivalent and exercised by the same parameterised
contract suite. The only thing SQLite cannot do is execute raw Cypher
patterns; it uses pre-translated SQL queries instead. If a future
feature needs ad-hoc Cypher, the Neo4j backend will get it first.

**What about embeddings?**
Both backends support the full hybrid-search stack (vector + fulltext +
graph boost + temporal). SQLite stores vectors via `sqlite-vec` in the
same `.db` file; Neo4j uses its native vector index.

If the graph backend is healthy but the embedding service is down,
Engrama degrades to fulltext search and reports that explicitly. This
lets you distinguish "Neo4j is misconfigured" from "Ollama / embeddings
are unavailable".

**Does the schema migration script (`engrama init`) work on SQLite?**
SQLite's schema lives in `engrama/backends/sqlite/schema.sql` and is
applied automatically when the database file is created. `engrama init`
on a SQLite backend is a no-op for schema (the Cypher constraints are
ignored) but still seeds the domain nodes from your profile.

**Where is my data on each backend?**

- **SQLite:** `~/.engrama/engrama.db` by default, or wherever
  `ENGRAMA_DB_PATH` points. Single file. Back it up with `cp`.
- **Neo4j:** inside the Docker volume `engrama_neo4j_data` (or wherever
  you mounted Neo4j's data directory). Back it up with
  `neo4j-admin database dump`.
