# Architecture

> Primary technical briefing document. Claude Code must read this before writing any code.

## Stack

| Component | Technology | Version | Reason |
|---|---|---|---|
| Database | Neo4j Community | 5.26.24 LTS | Free, local, supported until June 2028 |
| Language | Python | ≥ 3.11 | Agent ecosystem, FastMCP compatibility |
| Dependency mgmt | uv | latest | Modern standard, fast |
| MCP adapter | mcp-neo4j-cypher | scops fork | Full Cypher control |
| Container | Docker Desktop | latest | Reproducible infrastructure |
| CI/CD | GitHub Actions | — | Tests and PyPI publishing |
| Packaging | pyproject.toml | — | Installable as `pip install engrama` |

## Layer diagram

```
┌─────────────────────────────────────────────┐
│           Layer 1 · Adapters                │
│  MCP server · REST API · LangChain · SDK    │
├─────────────────────────────────────────────┤
│           Layer 2 · Skills library          │
│  remember · recall · associate · forget...  │
├─────────────────────────────────────────────┤
│           Layer 3 · Memory engine           │
│  write pipeline · query · vector · TTL      │
├─────────────────────────────────────────────┤
│           Layer 4 · Graph schema            │
│  nodes · relations · constraints · profiles │
├─────────────────────────────────────────────┤
│           Layer 5 · Neo4j 5.26 LTS          │
│  bolt://localhost:7687 · Docker Desktop     │
└─────────────────────────────────────────────┘
```

## Directory structure

```
engrama/
├── README.md
├── VISION.md
├── ARCHITECTURE.md
├── GRAPH-SCHEMA.md
├── ROADMAP.md
├── CONTRIBUTING.md
├── CHANGELOG.md
├── pyproject.toml
├── docker-compose.yml
├── .env.example
│
├── engrama/
│   ├── __init__.py          # public API: Engrama class
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── client.py        # Neo4j driver, connection pool, health check
│   │   ├── engine.py        # write pipeline (MERGE+timestamps), query, fulltext, TTL
│   │   └── schema.py        # Python dataclasses for nodes and relationships
│   │
│   ├── skills/
│   │   ├── __init__.py
│   │   ├── remember.py      # MERGE entity + observation
│   │   ├── recall.py        # fulltext search + graph traversal
│   │   ├── associate.py     # create relationships between entities
│   │   ├── reflect.py       # infer implicit relationships
│   │   ├── forget.py        # decay, archiving, TTL
│   │   └── summarize.py     # condense subgraph into synthesis node
│   │
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── mcp/             # MCP server (uses scops/mcp-neo4j-cypher)
│   │   ├── langchain/       # LangChain Memory + Tool
│   │   ├── rest/            # FastAPI HTTP endpoints
│   │   └── sdk/             # direct Python SDK, no server needed
│   │
│   └── ingest/
│       ├── __init__.py
│       ├── conversation.py  # extract entities from conversation transcripts
│       ├── document.py      # import from PDF, Markdown, Obsidian vault
│       └── web.py           # URLs, RSS feeds
│
├── profiles/
│   ├── developer.yaml
│   ├── researcher.yaml
│   └── assistant.yaml
│
├── scripts/
│   └── init-schema.cypher
│
├── examples/
│   ├── claude_desktop/
│   │   ├── config.json
│   │   └── system-prompt.md
│   └── langchain_agent/
│       └── example.py
│
└── tests/
    ├── conftest.py
    ├── test_core.py
    ├── test_skills.py
    └── test_adapters.py
```

## MCP adapter

The MCP adapter is the first to implement — it connects Engrama directly to Claude Desktop.

Uses `mcp-neo4j-cypher` from fork `scops/mcp-neo4j`, exposing three tools:
- `get-neo4j-schema` — introspect current graph schema
- `read-neo4j-cypher` — execute read queries
- `write-neo4j-cypher` — execute write queries

Any improvements made to the adapter should be contributed upstream to `neo4j-contrib/mcp-neo4j`.

## Profile system

A YAML profile fully defines the graph schema without writing code:

```yaml
# profiles/developer.yaml
name: developer
description: Profile for developers and technical instructors
nodes:
  - label: Project
    properties: [name, status, repo, stack, description]
    required: [name]
  - label: Technology
    properties: [name, version, type, notes]
    required: [name]
  - label: Decision
    properties: [title, rationale, date, alternatives]
    required: [title]
  - label: Problem
    properties: [title, solution, status, context]
    required: [title]
  - label: Course
    properties: [name, cohort, date, level, client]
    required: [name]
  - label: Concept
    properties: [name, domain, notes]
    required: [name]
  - label: Client
    properties: [name, sector, contact]
    required: [name]
relations:
  - {type: USES,        from: Project,    to: Technology}
  - {type: INFORMED_BY, from: Project,    to: Decision}
  - {type: HAS,         from: Project,    to: Problem}
  - {type: FOR,         from: Project,    to: Client}
  - {type: ORIGIN_OF,   from: Project,    to: Course}
  - {type: APPLIES,     from: Project,    to: Concept}
  - {type: SOLVED_BY,   from: Problem,    to: Decision}
  - {type: COVERS,      from: Course,     to: Concept}
  - {type: TEACHES,     from: Course,     to: Technology}
  - {type: IMPLEMENTS,  from: Technology, to: Concept}
```

## Implementation rules

1. **Always `MERGE`, never bare `CREATE`** — prevents duplicates
2. **Fulltext index is mandatory** — `memory_search` across all nodes and text properties
3. **Timestamps everywhere** — `created_at` and `updated_at` on every node, managed by engine
4. **No embeddings in v1** — structure first, vectors in v2
5. **Integration tests against a real Neo4j** — no mocks for the data layer
6. **Cypher parameters always** — never string-format queries (injection risk)

## Related repositories

- `scops/mcp-neo4j` — MCP adapter fork; improvements contributed upstream
- `scops/engrama` — this framework
