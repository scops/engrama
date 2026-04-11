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

### 1. Clone and configure credentials

```bash
git clone https://github.com/scops/engrama
cd engrama
cp .env.example .env
```

Open `.env` and **set your own password** for `NEO4J_PASSWORD`.
You can generate a secure one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

> **Security note:** The `.env` file contains your database credentials and
> is listed in `.gitignore` — it is never committed to the repository.
> The `.env.example` file ships with a sample password for convenience;
> replace it immediately in your local `.env` before starting Neo4j.

### 2. Start Neo4j

```bash
docker compose up -d
```

Wait ~15 seconds for the database to become ready, then initialise the schema:

```bash
# Linux / macOS / Git Bash
docker exec -i engrama-neo4j cypher-shell \
  -u neo4j -p "$(grep NEO4J_PASSWORD .env | cut -d= -f2)" \
  < scripts/init-schema.cypher

# PowerShell
Get-Content scripts/init-schema.cypher |
  docker exec -i engrama-neo4j cypher-shell `
    -u neo4j -p (Get-Content .env | Select-String 'NEO4J_PASSWORD' |
    ForEach-Object { $_.Line.Split('=',2)[1] })
```

Verify at [http://localhost:7474](http://localhost:7474) using the credentials from your `.env`.

### 3. Install and test

```bash
uv sync
uv run python -m pytest tests/test_core.py -v
```

### 4. Run the MCP server

```bash
uv run engrama-mcp
```

Or use it from Claude Desktop — see the MCP section below.

---

## MCP integration (Claude Desktop)

Engrama acts as an abstraction layer between the AI agent and the database.
Claude Desktop connects to the Engrama MCP server — it never sees database
credentials, connection strings, or raw queries.

Add to `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

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

Adjust `--directory` to wherever you cloned the repo.
No credentials needed here — the server reads them from `.env` internally.

Restart Claude Desktop. You'll get four tools: `engrama_search`,
`engrama_remember`, `engrama_relate`, and `engrama_context`.

See [`examples/claude_desktop/system-prompt.md`](examples/claude_desktop/system-prompt.md)
for a ready-to-paste system prompt that teaches Claude how to use the memory graph.

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

## License

MIT — see [LICENSE](LICENSE)

## Related

- [scops/mcp-neo4j](https://github.com/scops/mcp-neo4j) — our MCP adapter fork
- [neo4j-contrib/mcp-neo4j](https://github.com/neo4j-contrib/mcp-neo4j) — upstream
