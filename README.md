# Engrama

> Graph-based long-term memory framework for AI agents.

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![Neo4j](https://img.shields.io/badge/neo4j-5.26_LTS-green.svg)](https://neo4j.com)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/badge/pypi-engrama-orange.svg)](https://pypi.org/project/engrama)

Engrama gives any AI agent persistent, structured memory backed by a Neo4j knowledge graph. Instead of flat key-value stores or opaque vector databases, Engrama stores **entities**, **observations**, and **relationships** — and lets agents traverse that graph to reason about their accumulated knowledge.

Inspired by Karpathy's second brain concept, but built for agents rather than humans — and with graphs instead of wikis.

---

## Why graphs?

| | Flat JSON / KV | Vector DB | **Engrama (Graph)** |
|---|---|---|---|
| Relationship queries | ❌ | ❌ | ✅ native |
| Scales to 10k+ memories | ❌ slow | ✅ | ✅ |
| No embeddings required | ✅ | ❌ | ✅ |
| Local-first / private | ✅ | depends | ✅ |
| "What projects use FastMCP?" | full scan | approximate | 1-hop traversal |

---

## Prerequisites

You need three things installed before starting. If you already have them, skip to **Quick start**.

| Requirement | Version | How to check | Install guide |
|---|---|---|---|
| **Python** | 3.11 or newer | `python --version` | [python.org/downloads](https://www.python.org/downloads/) |
| **Docker Desktop** | any recent | `docker --version` | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) |
| **uv** (Python package manager) | any recent | `uv --version` | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |

> **Windows users:** after installing Python, make sure "Add Python to PATH"
> is checked.  After installing uv, you may need to restart your terminal.

Optional: [Obsidian](https://obsidian.md/) — needed only for vault sync features.

---

## Quick start

### Step 1: Clone the repository

```bash
git clone https://github.com/scops/engrama
cd engrama
```

### Step 2: Configure credentials

Copy the example environment file and set a password:

```bash
# Linux / macOS / Git Bash
cp .env.example .env

# PowerShell (Windows)
Copy-Item .env.example .env
```

Now open `.env` in any text editor and set **two values**:

1. `NEO4J_PASSWORD` — change `CHANGE_ME_BEFORE_FIRST_RUN` to a password of your choice
2. `VAULT_PATH` — the **absolute path** to your Obsidian vault folder
   (e.g. `VAULT_PATH=C:\Users\you\Documents\obsidian_vault\vault`)

`VAULT_PATH` is required for Obsidian sync tools (`engrama_sync_note`,
`engrama_sync_vault`, `engrama_write_insight_to_vault`).  If you don't use
Obsidian, you can leave it empty — the graph tools will still work.

### Step 3: Start Neo4j

```bash
docker compose up -d
```

Wait ~15 seconds for the database to start. You can check it's healthy with:

```bash
docker ps
```

You should see `engrama-neo4j` with status `Up ... (healthy)`.

### Step 4: Install dependencies

```bash
uv sync
```

This creates a virtual environment in `.venv/` and installs all dependencies.

### Step 5: Initialise the schema

This generates the graph schema from the developer profile and applies it to Neo4j:

```bash
uv run engrama init --profile developer
```

You should see:

```
Generating schema from developer.yaml...
Schema files generated.
Applying schema to Neo4j...
Schema applied successfully.
```

### Step 6: Verify everything works

```bash
uv run engrama verify
```

Expected output: `Connected to Neo4j at bolt://localhost:7687`

Optionally, run the test suite:

```bash
uv run pytest tests/ -v
```

### Step 7: Use it

You have three ways to use Engrama:

**A) From Claude Desktop** (recommended) — see the MCP section below.

**B) From Python:**

```python
from engrama import Engrama

with Engrama() as eng:
    eng.remember("Technology", "Neo4j", "Graph database for knowledge graphs")
    results = eng.search("Neo4j")
```

**C) From the command line:**

```bash
uv run engrama search "Neo4j"
uv run engrama reflect
```

> **Note:** all `engrama` CLI commands must be prefixed with `uv run`
> unless you activate the virtual environment first with
> `.venv\Scripts\Activate.ps1` (Windows) or `source .venv/bin/activate`
> (Linux/macOS).

### What's next?

The Quick Start sets you up with the default **developer** profile.  If you're
not a developer, or you want a graph that fits your specific workflow, see
the [Personalizing your graph](#personalizing-your-graph-onboarding) section below.

If you have existing Obsidian notes and want to populate the graph from them,
connect via Claude Desktop (next section) and ask Claude to run `engrama_sync_vault`.

---

## MCP integration (Claude Desktop)

Engrama acts as an abstraction layer between the AI agent and the database.
Claude Desktop connects to the Engrama MCP server — it never sees database
credentials, connection strings, or raw queries.

**1. Find your Claude Desktop config file:**

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

**2. Add the Engrama server.** Open the file and add (or merge into) the
`mcpServers` section:

```json
{
  "mcpServers": {
    "engrama": {
      "command": "uv",
      "args": [
        "run", "--directory", "C:\\Proyectos\\engrama",
        "--extra", "mcp", "engrama-mcp"
      ]
    }
  }
}
```

**Important:** change `C:\\Proyectos\\engrama` to the actual path where you
cloned the repo. On macOS/Linux use forward slashes (e.g. `/home/you/engrama`).
No database credentials are needed here — the server reads them from `.env`.

**3. Restart Claude Desktop** completely (quit and reopen, not just close the window).

You should now see the Engrama tools available. There are eleven:

| Tool | Description |
|------|-------------|
| `engrama_search` | Fulltext search across the memory graph |
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
for a ready-to-paste system prompt that teaches Claude how to use the memory graph.

---

## Python SDK

Use Engrama directly from any Python script — no MCP required:

```python
from engrama import Engrama

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

All methods are documented with docstrings — use `help(Engrama)` or your IDE
autocomplete to explore.

---

## CLI reference

All commands require `uv run` prefix (or an activated virtualenv):

```bash
uv run engrama init --profile developer                        # Standalone profile
uv run engrama init --profile base --modules hacking teaching  # Composable
uv run engrama init --profile developer --dry-run              # Preview without writing
uv run engrama verify                                          # Check Neo4j connectivity
uv run engrama search "microservices"                          # Fulltext search
uv run engrama reflect                                         # Run pattern detection
```

---

## Personalizing your graph (onboarding)

Engrama ships with a `developer` profile, but the graph schema should match
**your** world, not a generic template.  A nurse's graph looks nothing like a
developer's graph — and that's the point.

### Option A: Use the built-in developer profile

If you're a developer or technical instructor, the default profile already works:

```bash
uv run engrama init --profile developer
```

This creates nodes for Projects, Technologies, Decisions, Problems, Courses,
Concepts, and Clients.

### Option B: Let Claude build your modules (recommended)

This is the easiest path, and it works for **any** role or combination of
roles.  Open Claude Desktop with Engrama connected and say:

> "I want to set up Engrama for my work. I'm a nurse with a master in
> biology, I teach undergraduate students, and I love cooking on weekends."

Claude will interview you for about 5 minutes — what you track day to day,
how things connect in your head — and then generate custom domain modules
tailored to you: `nursing.yaml`, `biology.yaml`, `teaching.yaml`,
`cooking.yaml`.  It composes them with the universal `base.yaml` and applies
the schema, all in one conversation.  No YAML knowledge required.

### Option C: Compose from existing modules

Engrama ships with a few example modules to get you started.  Combine any of
them with the universal **base** profile:

```bash
uv run engrama init --profile base --modules hacking teaching photography ai
```

This merges `profiles/base.yaml` (Project, Concept, Decision, Problem,
Technology, Person) with domain-specific nodes and relations from
`profiles/modules/`.

**Included example modules:**

| Module | Adds |
|---|---|
| `hacking` | Target, Vulnerability, Technique, Tool, CTF |
| `teaching` | Course, Client, Exercise, Material |
| `photography` | Photo, Location, Species, Gear |
| `ai` | Model, Dataset, Experiment, Pipeline |

These four are **examples, not a closed list**.  The real power is that anyone
can create a module for any domain — see Option D below.

### Option D: Write your own module

A module is just a small YAML file in `profiles/modules/`.  Here's a complete
example for someone who tracks cooking:

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
  - {type: USES,      from: Recipe,           to: Ingredient}
  - {type: APPLIES,   from: Recipe,           to: CookingTechnique}
  - {type: RELATED,   from: Ingredient,       to: Concept}        # 'Concept' comes from base.yaml
  - {type: DOCUMENTS, from: Recipe,           to: Project}        # 'Project' comes from base.yaml
```

Save it as `profiles/modules/cooking.yaml`, then compose:

```bash
uv run engrama init --profile base --modules cooking teaching
```

**Rules for modules:**

- Nodes use PascalCase labels and `name` or `title` as the merge key
- Relations can reference any label in `base.yaml` (Project, Concept,
  Decision, Problem, Technology, Person) without redefining them
- If two modules define the same label, properties are merged automatically
- Relationship types should be verbs (USES, TREATS, COVERS), not nouns

See [`profiles/developer.yaml`](profiles/developer.yaml) for a complete
standalone profile, and
[`engrama/skills/onboard/references/example-profiles.md`](engrama/skills/onboard/references/example-profiles.md)
for worked profiles across very different domains (nurse, lawyer, PM,
freelance creative).

### Tips for good profiles

- **3 to 5 node types per module** is the sweet spot.  The base already gives
  you 6.  A typical multi-role user ends up with 12–18 total, which is fine.
- Use `title` as the merge key for sentence-like things (decisions, problems,
  protocols).  Use `name` for everything else.
- Always include `status` on nodes with a lifecycle — the reflect skill uses it
  to distinguish open vs resolved items.
- When in doubt, let Claude generate the module for you (Option B).

---

## Documentation

- [Vision](VISION.md) — why this exists
- [Architecture](ARCHITECTURE.md) — technical design and directory structure
- [Graph Schema](GRAPH-SCHEMA.md) — nodes, relationships, Cypher reference
- [Roadmap](ROADMAP.md) — development phases and status
- [Contributing](CONTRIBUTING.md) — how to contribute

---

## License

MIT — see [LICENSE](LICENSE)

## Related

- [neo4j-contrib/mcp-neo4j](https://github.com/neo4j-contrib/mcp-neo4j) — Neo4j MCP server (Engrama uses its own native adapter instead)
