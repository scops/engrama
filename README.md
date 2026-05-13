# Engrama

> Graph-based long-term memory framework for AI agents.

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![Backend](https://img.shields.io/badge/backend-SQLite_%7C_Neo4j-green.svg)](BACKENDS.md)
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

The data model is identical on both. See **[BACKENDS.md](BACKENDS.md)**
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
- A local embedder for semantic search — Ollama, LM Studio, vLLM,
  llama.cpp, or any service that speaks the OpenAI embeddings API. See
  [Embedding setup](#embedding-setup-optional).
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

This creates a virtual environment in `.venv/` and installs the base
dependencies (`sqlite-vec`, `httpx`, `pydantic`, `python-dotenv`,
`pyyaml`). The Neo4j driver is **not** installed by default.

### Step 2: Initialise the schema

```bash
uv run engrama init --profile developer
```

The SQLite database file is created on first use under
`~/.engrama/engrama.db`. The schema is applied automatically — no
constraints to run, no service to wait for. Domain seed nodes from your
profile are loaded.

### Step 3: Verify

```bash
uv run engrama verify
```

Expected output: `backend=sqlite, ok=true, ...`

### Step 4: Use it

Three ways:

**A) From Claude Desktop** — see [MCP integration](#mcp-integration-claude-desktop) below.

**B) From Python:**

```python
from engrama import Engrama

with Engrama() as eng:
    eng.remember("Technology", "FastAPI", "High-performance async framework")
    eng.associate("MyProject", "Project", "USES", "FastAPI", "Technology")
    results = eng.search("microservices")
```

**C) From the command line:**

```bash
uv run engrama search "FastAPI"
uv run engrama reflect
```

> **Note:** all `engrama` CLI commands need the `uv run` prefix unless
> you activate the virtual environment first with
> `.venv\Scripts\Activate.ps1` (Windows) or `source .venv/bin/activate`
> (Linux/macOS).

---

## Quick start (Neo4j, opt-in)

If you've read [BACKENDS.md](BACKENDS.md) and decided you need Neo4j —
multi-process writes, very large vector indexes, an existing Cypher
toolchain — follow this path instead.

### Step 1: Install with the Neo4j extra

```bash
git clone https://github.com/scops/engrama
cd engrama
uv sync --extra neo4j
```

### Step 2: Configure credentials

```bash
# Linux / macOS / Git Bash
cp .env.example .env
# PowerShell (Windows)
Copy-Item .env.example .env
```

Open `.env` and set:

1. `GRAPH_BACKEND=neo4j`
2. `NEO4J_PASSWORD` — choose a strong password
3. `VAULT_PATH` (optional) — absolute path to your Obsidian vault if
   you want vault sync features

### Step 3: Start Neo4j

```bash
docker compose up -d
```

Wait ~15 seconds. Verify with `docker ps` — `engrama-neo4j` should be
`healthy`.

### Step 4: Initialise the schema

```bash
uv run engrama init --profile developer
```

This generates and applies Cypher constraints + the fulltext and vector
indexes.

### Step 5: Verify

```bash
uv run engrama verify
```

Expected output: `Connected to Neo4j at bolt://localhost:7687`.

If you set `GRAPH_BACKEND=neo4j` but only installed the base
dependencies, `uv run engrama verify` fails with an explicit hint to
run `uv sync --extra neo4j`.

The rest of the workflow (Python SDK, CLI, MCP integration) is
identical to the SQLite path.

---

## Embedding setup (optional)

Engrama works out of the box with fulltext search only. For **semantic
similarity search** — finding conceptually related nodes, not just
keyword matches — enable embeddings via any OpenAI-compatible service.

Set four env vars (`EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`,
`EMBEDDING_DIMENSIONS`, `OPENAI_BASE_URL`) plus `OPENAI_API_KEY` when
the server expects one. Then run `uv run engrama verify` — it prints
`Embeddings: ok (provider=…, model=…)` on success and
`Embeddings: degraded …` if the endpoint or model is unreachable.

After enabling embeddings on an existing graph, run
`uv run engrama reindex` to embed nodes that were created before. New
nodes are embedded automatically on creation. If the endpoint goes
away later, search degrades to `fulltext_only` and surfaces the reason
in `search_mode` — Engrama never silently returns empty results.

### Provider matrix (worked examples)

Every provider below speaks the OpenAI `/v1/embeddings` shape, so
`EMBEDDING_PROVIDER=openai` is the recommended setting for all of them
(including local Ollama). The Ollama-native `/api/embed` path is also
supported via `EMBEDDING_PROVIDER=ollama` — kept for backward
compatibility, but pick one style and stay there.

#### Ollama (local, recommended starter)

Local, free, no API key, ~274 MB model download. Best path if you just
want to try semantic search.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMENSIONS=768
OPENAI_BASE_URL=http://localhost:11434/v1
```

```bash
# Install: https://ollama.com
ollama pull nomic-embed-text
uv run engrama verify   # → "Embeddings: ok (provider=openai, model=nomic-embed-text)"
```

Other strong local models: `mxbai-embed-large` (1024 dims, English),
`bge-m3` (1024 dims, multilingual). Match `EMBEDDING_DIMENSIONS` to the
model — mismatches make hybrid search drop to fulltext.

#### OpenAI

Cloud API, paid, sub-10 ms latency. The reference implementation of
the `/v1/embeddings` contract.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
```

`text-embedding-3-large` (3072 dims) is also supported — set
`EMBEDDING_DIMENSIONS=3072` to match. `text-embedding-3-*` models
accept a smaller dimensions value to truncate (e.g. set
`EMBEDDING_DIMENSIONS=512` for the small model and OpenAI returns
512-dim vectors).

#### LM Studio

GUI-managed local server, useful when you want a model-picker UI and
download manager. Start an embedding model from LM Studio's "Local
Server" tab first.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=nomic-ai/nomic-embed-text-v1.5-GGUF
EMBEDDING_DIMENSIONS=768
OPENAI_BASE_URL=http://localhost:1234/v1
OPENAI_API_KEY=lm-studio
```

LM Studio ignores the API key value but its HTTP client expects the
header to be present — any non-empty string works.

#### vLLM

High-throughput inference server, good fit when you embed in bulk and
want batching to a GPU.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=intfloat/e5-mistral-7b-instruct
EMBEDDING_DIMENSIONS=4096
OPENAI_BASE_URL=http://localhost:8000/v1
OPENAI_API_KEY=any
```

Start vLLM with an embeddings-capable model:
`vllm serve intfloat/e5-mistral-7b-instruct --task embed`. Match
`EMBEDDING_DIMENSIONS` to the model's hidden size.

#### llama.cpp server

Single-binary CPU/GPU server, minimal moving parts. Useful for tiny
embedding models on resource-constrained hosts.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=nomic-embed-text-v1.5.Q4_K_M
EMBEDDING_DIMENSIONS=768
OPENAI_BASE_URL=http://localhost:8080/v1
OPENAI_API_KEY=any
```

Start with `--embedding` and pass the GGUF model path:
`./llama-server -m nomic-embed-text-v1.5.Q4_K_M.gguf --embedding --port 8080`.

#### Jina (cloud)

Hosted multilingual embeddings with long context. Pay-per-token, no
self-hosting.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=jina-embeddings-v3
EMBEDDING_DIMENSIONS=1024
OPENAI_BASE_URL=https://api.jina.ai/v1
OPENAI_API_KEY=jina_...
```

`jina-embeddings-v3` is multilingual (89 languages) with 8192-token
context. For shorter inputs and tighter latency, use
`jina-embeddings-v2-base-en` with `EMBEDDING_DIMENSIONS=768`.

---

## MCP integration (Claude Desktop)

Engrama acts as an abstraction layer between the AI agent and the
storage backend. Claude Desktop connects to the Engrama MCP server — it
never sees database credentials, connection strings, or raw queries.

**1. Find your Claude Desktop config file:**

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

**2. Add the Engrama server.**

The config below uses the SQLite default. The `--backend` flag is
optional (defaults to `sqlite`) but explicit is friendlier:

```json
{
  "mcpServers": {
    "engrama": {
      "command": "uv",
      "args": [
        "run", "--directory", "C:\\Proyectos\\engrama",
        "--extra", "mcp",
        "engrama-mcp", "--backend", "sqlite"
      ]
    }
  }
}
```

For the Neo4j backend swap `--backend sqlite` for `--backend neo4j` (or
omit the flag and set `GRAPH_BACKEND=neo4j` in `.env`). Make sure
`--extra mcp` is replaced or augmented with `--extra neo4j` too:
`"--extra", "mcp", "--extra", "neo4j"`.

If you forget the `neo4j` extra while keeping `GRAPH_BACKEND=neo4j`, the
MCP tools now return the startup cause instead of only
`Async store not initialised`.

**Important:** change `C:\\Proyectos\\engrama` to the actual path where
you cloned the repo. On macOS/Linux use forward slashes (e.g.
`/home/you/engrama`). No database credentials are needed in this file —
the server reads them from `.env` when running against Neo4j.

**3. Restart Claude Desktop** completely (quit and reopen).

You should now see the eleven Engrama tools:

| Tool | Description |
|------|-------------|
| `engrama_search` | Hybrid search (vector + fulltext + graph boost + temporal) |
| `engrama_remember` | Create or update a node (always MERGE) |
| `engrama_relate` | Create a relationship between two nodes |
| `engrama_context` | Retrieve the neighbourhood of a node |
| `engrama_sync_note` | Sync a single Obsidian note to the graph |
| `engrama_sync_vault` | Full vault scan, reconcile all notes |
| `engrama_ingest` | Read content + extract knowledge automatically |
| `engrama_reflect` | Adaptive cross-entity pattern detection → Insights |
| `engrama_surface_insights` | Read pending Insights for review |
| `engrama_approve_insight` | Approve or dismiss an Insight |
| `engrama_write_insight_to_vault` | Write approved Insight to Obsidian |

See [`examples/claude_desktop/system-prompt.md`](examples/claude_desktop/system-prompt.md)
for a ready-to-paste system prompt that teaches Claude how to use the
memory graph.

---

## Python SDK

Use Engrama directly from any Python script — no MCP required:

```python
from engrama import Engrama

# Defaults to SQLite at ~/.engrama/engrama.db
with Engrama() as eng:
    # Write
    eng.remember("Technology", "FastAPI", "High-performance async framework")
    eng.associate("MyProject", "Project", "USES", "FastAPI", "Technology")

    # Read
    results = eng.recall("FastAPI", hops=2)
    hits = eng.search("microservices", limit=5)

    # Reflect
    insights = eng.reflect()
    pending = eng.surface_insights()
    eng.approve_insight(pending[0].title)

    # Forget
    eng.forget("Technology", "OldLib")
    eng.forget_by_ttl("Technology", days=365, purge=True)
```

To target Neo4j explicitly:

```python
with Engrama(backend="neo4j") as eng:
    ...
```

Or set `GRAPH_BACKEND=neo4j` in `.env` and call `Engrama()` with no
arguments. All methods are documented with docstrings — use
`help(Engrama)` or your IDE autocomplete to explore.

---

## CLI reference

All commands need the `uv run` prefix (or an activated virtualenv):

```bash
uv run engrama init --profile developer                         # SQLite (default)
uv run engrama init --profile base --modules hacking teaching   # Composable
uv run engrama init --profile developer --dry-run               # Preview
uv run engrama verify                                           # Health check
uv run engrama search "microservices"                           # Hybrid search
uv run engrama reflect                                          # Pattern detection
uv run engrama reindex                                          # Re-embed all nodes
uv run engrama decay --dry-run                                  # Preview decay
uv run engrama decay --rate 0.01                                # Apply gentle decay
uv run engrama decay --rate 0.1 --min-confidence 0.05           # Aggressive + archive
```

To override the backend on a single command:

```bash
GRAPH_BACKEND=neo4j uv run engrama verify
```

`engrama verify` also checks the embedding provider when one is
configured. A healthy backend with a down embedder reports
`Embeddings: degraded ...`, which separates storage failures from
semantic-search failures.

---

## Troubleshooting

**`No module named 'neo4j'` or an error telling you to install the extra**

Your config points at Neo4j but the Python driver is not installed.
Fix it with:

```bash
uv sync --extra neo4j
```

If you launch the MCP server through `uv run`, include both extras when
needed:

```bash
uv run --extra mcp --extra neo4j engrama-mcp --backend neo4j
```

**MCP says `Async store not initialised`**

That message now includes the root cause. The common causes are:

- `GRAPH_BACKEND=neo4j` but the `neo4j` extra is missing
- `NEO4J_PASSWORD` is unset
- Neo4j is not reachable at `NEO4J_URI`

**Search works but `search_mode` says `fulltext_only` with `degraded=true`**

The graph backend is up, but embeddings are not. Common causes:

- Ollama is not running
- the configured embedding model has not been pulled yet
- the endpoint does not match the selected provider

For native Ollama mode use:

```dotenv
EMBEDDING_PROVIDER=ollama
OLLAMA_URL=http://localhost:11434
EMBEDDING_MODEL=nomic-embed-text
```

For OpenAI-compatible mode against Ollama use:

```dotenv
EMBEDDING_PROVIDER=openai
OPENAI_BASE_URL=http://localhost:11434/v1
EMBEDDING_MODEL=nomic-embed-text
```

---

## Search modes

Three modes, controlled by `EMBEDDING_PROVIDER`:

**Fulltext only** (`EMBEDDING_PROVIDER=none`, default) — keyword
matching. SQLite uses FTS5; Neo4j uses its native fulltext index.
Works without any extra dependency.

**Hybrid** (`EMBEDDING_PROVIDER=ollama` or `openai`) — combines
semantic similarity (vector search) with keyword matching plus a graph
topology boost and a temporal recency factor. Finds conceptually
related nodes even without exact keyword overlap.

**Activation:**
1. Set `EMBEDDING_PROVIDER` in `.env` (see
   [Embedding setup](#embedding-setup-optional)).
2. Run `uv run engrama reindex` to embed existing nodes.
3. New nodes are embedded automatically on creation.

The scoring formula is:

    final = α × vector + (1-α) × fulltext + β × graph_boost + γ × temporal

with α=0.6, β=0.15, γ=0.1 by default. Tune via `HYBRID_ALPHA` and
`HYBRID_GRAPH_BETA` in `.env`.

---

## Personalising your graph (onboarding)

Engrama ships with a `developer` profile, but the schema should match
**your** world, not a generic template. A nurse's graph looks nothing
like a developer's graph — and that's the point.

### Option A: Use the built-in `developer` profile

```bash
uv run engrama init --profile developer
```

Creates nodes for Projects, Technologies, Decisions, Problems, Courses,
Concepts, and Clients.

### Option B: Let Claude build your modules (recommended)

Open Claude Desktop with Engrama connected and say:

> "I want to set up Engrama for my work. I'm a nurse with a master in
> biology, I teach undergraduate students, and I love cooking on
> weekends."

Claude will interview you for ~5 minutes — what you track day to day,
how things connect in your head — and then generate custom domain
modules: `nursing.yaml`, `biology.yaml`, `teaching.yaml`, `cooking.yaml`.
It composes them with the universal `base.yaml` and applies the schema,
all in one conversation. No YAML knowledge required.

### Option C: Compose from existing modules

```bash
uv run engrama init --profile base --modules hacking teaching photography ai
```

This merges `profiles/base.yaml` (Project, Concept, Decision, Problem,
Technology, Person) with domain-specific modules from
`profiles/modules/`.

**Included example modules:**

| Module | Adds |
|---|---|
| `hacking` | Target, Vulnerability, Technique, Tool, CTF |
| `teaching` | Course, Client, Exercise, Material |
| `photography` | Photo, Location, Species, Gear |
| `ai` | Model, Dataset, Experiment, Pipeline |

These four are **examples, not a closed list** — anyone can create a
module for any domain.

### Option D: Write your own module

A module is a small YAML file in `profiles/modules/`. Example for
cooking:

```yaml
name: cooking
description: Recipes, techniques, and ingredients

nodes:
  - label: Recipe
    properties: [name, cuisine, difficulty, time, notes]
    required: [name]
    description: "A dish or preparation."
  - label: Ingredient
    properties: [name, category, season, notes]
    required: [name]
    description: "A food ingredient — vegetable, spice, protein."
  - label: CookingTechnique
    properties: [name, type, notes]
    required: [name]
    description: "A culinary method — sous vide, fermentation, braising."

relations:
  - {type: USES,      from: Recipe,     to: Ingredient}
  - {type: APPLIES,   from: Recipe,     to: CookingTechnique}
  - {type: RELATED,   from: Ingredient, to: Concept}        # 'Concept' from base.yaml
  - {type: DOCUMENTS, from: Recipe,     to: Project}        # 'Project' from base.yaml
```

Save as `profiles/modules/cooking.yaml`, then:

```bash
uv run engrama init --profile base --modules cooking teaching
```

**Module rules:**

- Nodes use PascalCase labels and `name` or `title` as the merge key.
- Relations can reference any label in `base.yaml` without redefining
  it.
- Two modules defining the same label have their properties merged.
- Relationship types should be verbs (USES, TREATS, COVERS), not nouns.

See [`profiles/developer.yaml`](profiles/developer.yaml) for a complete
standalone profile, and
[`engrama/skills/onboard/references/example-profiles.md`](engrama/skills/onboard/references/example-profiles.md)
for worked profiles in nursing, law, project management, freelance
creative.

### Tips for good profiles

- **3–5 node types per module** is the sweet spot. The base already
  gives you 6. A typical multi-role user ends up with 12–18 total.
- Use `title` as the merge key for sentence-like things (decisions,
  problems, protocols). Use `name` for everything else.
- Always include `status` on nodes with a lifecycle — reflect uses it
  to distinguish open vs resolved items.
- When in doubt, let Claude generate the module for you (Option B).

---

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `GRAPH_BACKEND` | `sqlite` | `sqlite`, `neo4j`, or `null` (testing) |
| `VECTOR_BACKEND` | matches graph | Auto-inferred (`sqlite-vec` for SQLite) |
| `ENGRAMA_DB_PATH` | `~/.engrama/engrama.db` | SQLite database file |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection URI |
| `NEO4J_USERNAME` | `neo4j` | Neo4j user |
| `NEO4J_PASSWORD` | — | Neo4j password (required when `GRAPH_BACKEND=neo4j`) |
| `NEO4J_DATABASE` | `neo4j` | Neo4j database name |
| `ENGRAMA_PROFILE` | `developer` | Profile for schema generation |
| `VAULT_PATH` | `~/Documents/vault` | Obsidian vault root path |
| `EMBEDDING_PROVIDER` | `none` | `none`, `ollama`, or `openai` |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Model name |
| `EMBEDDING_DIMENSIONS` | `768` | Embedding vector size |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compat endpoint |
| `OPENAI_API_KEY` | — | API key (when needed) |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `HYBRID_ALPHA` | `0.6` | Vector vs fulltext weight |
| `HYBRID_GRAPH_BETA` | `0.15` | Graph topology boost weight |

---

## Documentation

- [Vision](VISION.md) — why this exists
- [Backends](BACKENDS.md) — SQLite vs Neo4j decision guide
- [Architecture](ARCHITECTURE.md) — technical design and directory structure
- [Graph Schema](GRAPH-SCHEMA.md) — nodes, relationships, query reference
- [Roadmap](ROADMAP.md) — development phases and status
- [Changelog](CHANGELOG.md) — release notes
- [Contributing](CONTRIBUTING.md) — how to contribute
- [DDR-001](DDR-001.md) — faceted classification
- [DDR-002](DDR-002.md) — bidirectional vault sync
- [DDR-003](DDR-003.md) — protocol layer, embeddings, hybrid search, temporal reasoning
- [DDR-004](DDR-004.md) — portable storage (SQLite default)

---

## License

Engrama is licensed under the Apache License 2.0.
Copyright 2026 Sinensia IT Solutions.

You are free to use, modify, and distribute Engrama in personal and
commercial projects. The Apache 2.0 license includes an explicit patent
grant, giving you confidence to adopt Engrama in enterprise
environments without IP concerns.

### Contributing

By submitting a pull request you agree that your contribution is
licensed under the same Apache 2.0 terms. We use a Developer
Certificate of Origin (DCO) — sign off your commits with `git commit -s`.

### Commercial extensions

Premium features (managed hosting, multi-tenant collaboration,
advanced analytics) may be offered under a separate commercial license.
The core engine, MCP tools, and all community-facing functionality
remain fully open source under Apache 2.0.

For commercial licensing inquiries, contact
sinensiaitsolutions@gmail.com.

---

## Related

- [neo4j-contrib/mcp-neo4j](https://github.com/neo4j-contrib/mcp-neo4j) — generic Neo4j MCP server (Engrama uses its own native adapter that speaks both SQLite and Neo4j).
- [sqlite-vec](https://github.com/asg017/sqlite-vec) — SQLite extension for vector search; powers the default Engrama backend.
