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

## Quick start

```bash
# 1. Start Neo4j
docker compose up -d

# 2. Install Engrama
pip install engrama

# 3. Initialise schema
engrama init --profile developer

# 4. Use it
python -c "
from engrama import Engrama
brain = Engrama()
brain.remember('Project', 'my-api', 'Uses FastAPI and Neo4j')
print(brain.recall('FastAPI'))
"
```

---

## MCP integration (Claude Desktop)

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "engrama": {
      "command": "uvx",
      "args": ["mcp-neo4j-cypher"],
      "env": {
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "changeme123"
      }
    }
  }
}
```

Restart Claude Desktop. Claude can now read and write your knowledge graph directly from the conversation.

---

## Profiles

Profiles define your graph schema without touching code:

```bash
engrama init --profile developer    # Projects, Technologies, Decisions, Problems
engrama init --profile researcher   # Papers, Concepts, Authors, Hypotheses
engrama init --profile assistant    # People, Preferences, Tasks, Contexts
```

---

## Documentation

- [Vision](VISION.md) — why this exists
- [Architecture](ARCHITECTURE.md) — technical design and directory structure
- [Graph Schema](GRAPH-SCHEMA.md) — nodes, relationships, Cypher reference
- [Roadmap](ROADMAP.md) — development phases and status
- [Contributing](CONTRIBUTING.md) — how to contribute

---

## Setup

### Prerequisites

- Docker Desktop (Windows/Mac/Linux)
- Python 3.11+
- `uv` package manager

### Full setup

```bash
git clone https://github.com/scops/engrama
cd engrama
cp .env.example .env
docker compose up -d
uv sync
engrama init --profile developer
```

Verify at [http://localhost:7474](http://localhost:7474) (neo4j / changeme123).

---

## License

MIT — see [LICENSE](LICENSE)

## Related

- [scops/mcp-neo4j](https://github.com/scops/mcp-neo4j) — our MCP adapter fork
- [neo4j-contrib/mcp-neo4j](https://github.com/neo4j-contrib/mcp-neo4j) — upstream
