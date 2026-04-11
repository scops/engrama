# Roadmap

## Phase 0 · Setup & design ✅

- [x] Full architecture design
- [x] Name chosen — `engrama` (available on PyPI)
- [x] Initial documentation written
- [x] Project structure created at `C:\Proyectos\engrama`
- [ ] Create repo `github.com/scops/engrama`
- [ ] Configure Docker Desktop with Neo4j 5.26.24
- [ ] Configure filesystem + GitHub MCPs in Claude Desktop
- [ ] Verify bolt://localhost:7687 connectivity

## Phase 1 · Core (MVP)

> Goal: Claude Desktop can read and write the graph from within the conversation.

- [ ] `engrama/core/client.py` — Neo4j driver, connection pool, health check
- [ ] `scripts/init-schema.cypher` — constraints + fulltext index
- [ ] `engrama/core/engine.py` — write pipeline (MERGE + timestamps), basic query
- [ ] `engrama/core/schema.py` — Python dataclasses for nodes and relations
- [ ] `profiles/developer.yaml` — first complete profile
- [ ] Basic integration tests against a real Neo4j instance
- [ ] Verification: run init script, query schema from Neo4j Browser

## Phase 2 · MCP adapter

> Goal: use the graph from Claude Desktop via MCP without writing Cypher manually.

- [ ] Review `scops/mcp-neo4j` fork
- [ ] `engrama/adapters/mcp/` — integrate mcp-neo4j-cypher as adapter
- [ ] `examples/claude_desktop/config.json` — ready-to-paste config
- [ ] `examples/claude_desktop/system-prompt.md` — memory system prompt
- [ ] End-to-end test: Claude Desktop → MCP → Neo4j → response

## Phase 3 · Skills library

- [ ] `skills/remember.py` — `remember(entity_type, name, observation)`
- [ ] `skills/recall.py` — `recall(query, hops=2)`
- [ ] `skills/associate.py` — `associate(from_name, relation, to_name)`
- [ ] `skills/forget.py` — archiving by TTL or name
- [ ] `skills/reflect.py` — infer implicit relationships
- [ ] `skills/summarize.py` — condense subgraph into synthesis node
- [ ] Unit tests for each skill

## Phase 4 · Python SDK

- [ ] `engrama/adapters/sdk/__init__.py` — clean public API
- [ ] Usage documentation with examples
- [ ] Publish `engrama` to PyPI (v0.1.0)

## Phase 5 · Ingestion

- [ ] `ingest/conversation.py` — extract entities from transcripts
- [ ] `ingest/document.py` — import from Obsidian vault
- [ ] Bootstrap: import current active projects

## Phase 6 · Additional adapters

- [ ] `adapters/langchain/` — LangChain Memory + Tool
- [ ] `adapters/rest/` — FastAPI HTTP endpoints
- [ ] `examples/n8n_workflow/`

## Phase 7 · Vectors (v2)

- [ ] Vector index in Neo4j 5.26
- [ ] Local embeddings (ollama / nomic-embed-text)
- [ ] Hybrid search: graph + vector

## Definition of done

1. Code committed to repo
2. Test passes against real Neo4j
3. Documented in reference file
4. Conventional commit message
