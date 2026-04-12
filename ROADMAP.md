# Roadmap

## Phase 0 · Setup & design ✅

- [x] Full architecture design
- [x] Name chosen — `engrama` (available on PyPI)
- [x] Initial documentation written
- [x] Project structure created at `C:\Proyectos\engrama`
- [x] Neo4j running via Docker Desktop
- [x] Obsidian MCP integration designed
- [x] First memories loaded from Obsidian notes
- [x] Bug found: `engrama_relate` fails for Decision/Problem nodes
- [ ] Create repo `github.com/scops/engrama` → see Phase 7 for push checklist

## Phase 1 · Core (MVP) ✅

> Goal: Claude Desktop reads and writes the graph from within the conversation.

- [x] `engrama/core/client.py` — Neo4j driver, connection pool, health check
- [x] `scripts/init-schema.cypher` — constraints + fulltext index
- [x] `engrama/core/engine.py` — write pipeline (MERGE + timestamps), basic query
- [x] `engrama/core/schema.py` — Python dataclasses for nodes and relations
- [x] `profiles/developer.yaml` — complete profile with node descriptions
- [x] Basic integration tests against real Neo4j

## Phase 2 · MCP adapter ✅

> Goal: use the graph from Claude Desktop via MCP without writing Cypher manually.

- [x] `engrama/adapters/mcp/server.py` — native MCP server via FastMCP + async Neo4j driver
- [x] Ten MCP tools: search, remember, relate, context, sync_note, sync_vault, reflect, surface_insights, approve_insight, write_insight_to_vault
- [x] `examples/claude_desktop/config.json` — ready-to-paste config
- [x] `examples/claude_desktop/system-prompt.md` — memory system prompt
- [ ] End-to-end test: Claude Desktop → MCP → Neo4j → response (manual verification done, automated test pending)

> **Architectural decision (2026-04-11):** dropped `scops/mcp-neo4j` fork.
> The server talks to Neo4j directly via the official `neo4j` async driver —
> no intermediate MCP-to-Cypher layer needed.  This eliminates a dependency,
> removes a layer of indirection, and gives full control over MERGE logic,
> parameter handling, and the title-vs-name key distinction.

## Phase 3 · Obsidian sync ✅

> Goal: Obsidian vault ↔ Neo4j graph stay in sync automatically.

- [x] `engrama/adapters/obsidian/adapter.py` — vault file I/O wrapper
- [x] `engrama/adapters/obsidian/parser.py` — entity extraction from notes
- [x] `engrama/adapters/obsidian/sync.py` — bidirectional sync via engrama_id
- [x] `tests/test_obsidian_sync.py` — adapter + parser tests (11 tests)
- [x] `engrama_sync_note` + `engrama_sync_vault` MCP tools
- [x] ~~`has_document` flags~~ — superseded: sync parses note content directly, no per-type flag needed
- [x] ~~`full_scan()`~~ — superseded by `engrama_sync_vault` MCP tool (iterates all notes)
- [x] ~~`archive_missing()`~~ — superseded by `ForgetSkill` (Phase 4) which handles archiving by name or TTL

## Phase 4 · Skills base ✅

> Goal: four composable skill classes that agents can call directly.

- [x] `skills/remember.py` — `RememberSkill.run(engine, label, name, observation, extra)`
  - Auto-detects merge key (name vs title) via `TITLE_KEYED_LABELS`
  - Returns created/updated status
- [x] `skills/recall.py` — `RecallSkill.run(engine, query, limit, hops)`
  - Fulltext search → seed nodes → graph expansion up to N hops
  - Deduplicates neighbours, returns `RecallResult` dataclasses with properties + neighbour chain
- [x] `skills/associate.py` — `AssociateSkill.run(engine, from_name, from_label, rel_type, to_name, to_label)`
  - Validates labels and relationship types against schema enums
  - Delegates to `engine.merge_relation()`
- [x] `skills/forget.py` — `ForgetSkill.forget_by_name()` + `forget_by_ttl()`
  - Soft-delete (archive) by default — sets `status: "archived"` + `archived_at`
  - `purge=True` for permanent `DETACH DELETE`
  - TTL mode: archive/purge nodes older than N days by `updated_at`
- [x] Integration tests: 19 tests in `tests/test_phase4_skills.py`

## Phase 5 · reflect ✅

> Goal: cross-entity pattern detection without being asked.

- [x] `skills/reflect.py` — `ReflectSkill` with three multi-hop Cypher queries:
  - Cross-project solution transfer (Problem ↔ Concept ↔ Decision)
  - Shared technology between active Projects
  - Training opportunity (Problem ↔ Concept ↔ Course)
- [x] `Insight` node type added to schema + fulltext index + constraint
- [x] reflect writes Insight nodes with confidence score + status: "pending"
- [x] `engrama_reflect` MCP tool — agents can trigger reflect on demand
- [x] Tests with seeded graph data (4 tests in `test_skills.py`)

### Bugs fixed during Phase 5

- [x] `engrama_relate` — now matches by `title` for Decision/Problem nodes
      (fixed in both `server.py` and `engine.py`)
- [x] `test_obsidian_sync.py` — fixed walrus operator syntax error on line 62
- [x] Tests: 3 tests in `test_adapters.py` for relate title-key fix

## Phase 6 · proactive ✅

> Goal: surface Insights to the agent + write them back to Obsidian.

- [x] `skills/proactive.py` — `ProactiveSkill` with four methods:
  - `surface(engine, limit)` — reads pending Insights, newest first
  - `approve(engine, title)` — sets status to "approved" + `approved_at`
  - `dismiss(engine, title)` — sets status to "dismissed" + `dismissed_at`
  - `write_to_vault(engine, obsidian, title, target_note)` — appends approved Insight as markdown section
- [x] Three new MCP tools:
  - `engrama_surface_insights` — read pending Insights for agent presentation
  - `engrama_approve_insight` — human approves or dismisses (action: "approve" | "dismiss")
  - `engrama_write_insight_to_vault` — append approved Insight to Obsidian note
- [x] Insight lifecycle enforced: only approved Insights can be written to vault
- [x] Synced Insights get `obsidian_path` + `synced_at` in Neo4j
- [x] Integration tests: 12 tests in `tests/test_proactive.py`

## Phase 7 · Python SDK + PyPI ✅

> Goal: clean public API + CLI for non-MCP usage.

- [x] `engrama/adapters/sdk/__init__.py` — `Engrama` class wrapping all skills:
  - `remember()`, `recall()`, `search()`, `associate()`
  - `forget()`, `forget_by_ttl()`
  - `reflect()`, `surface_insights()`, `approve_insight()`, `dismiss_insight()`
  - `write_insight_to_vault()` (requires Obsidian)
  - Context manager, `verify()`, `has_vault`, `repr()`
- [x] `engrama/__init__.py` — top-level re-export: `from engrama import Engrama`
- [x] `engrama/cli.py` — four CLI commands:
  - `engrama init --profile developer [--dry-run] [--no-apply]` — codegen + schema apply
  - `engrama verify` — Neo4j connectivity check
  - `engrama reflect` — run pattern detection, print results
  - `engrama search <query>` — fulltext search
- [x] Integration tests: `test_sdk.py` (14 tests) + `test_cli.py` (6 tests)
- [ ] Manual end-to-end test: `engrama init --profile developer`, verify MCP tools in Claude Desktop
- [ ] Push to `github.com/scops/engrama` (phases 1–7 accumulated, not yet pushed)
- [ ] Publish `engrama` to PyPI (v0.1.0) — after repo push

## Phase 8 · Composable profiles ✅

> Goal: support multi-role users with modular, composable graph schemas.

- [x] `profiles/base.yaml` — universal base with Project, Concept, Decision, Problem, Technology, Person
- [x] `profiles/modules/hacking.yaml` — Target, Vulnerability, Technique, Tool, CTF
- [x] `profiles/modules/teaching.yaml` — Course, Client, Exercise, Material
- [x] `profiles/modules/photography.yaml` — Photo, Location, Species, Gear
- [x] `profiles/modules/ai.yaml` — Model, Dataset, Experiment, Pipeline
- [x] `scripts/generate_from_profile.py` — `merge_profiles()` function + `--modules` flag
  - Merges nodes by label (property union, longer description wins)
  - Deduplicates relations by (type, from, to)
  - Validates all relation endpoints exist in merged node set
- [x] CLI: `uv run engrama init --profile base --modules hacking teaching photography ai`
- [x] Backward compatible: standalone `--profile developer` still works
- [x] Onboard skill updated: documents composable approach + module YAML template
- [x] `example-profiles.md` updated with composable section
- [x] Integration tests: `tests/test_composable.py` — merge logic (9), codegen (3), real files (5), CLI (4)

## Phase 9 · Core features ✅

> Goal: make Engrama discover what the user didn't know they knew.

### 9a — Ingestion ✅
- [x] `engrama_ingest` MCP tool — reads vault note, raw text, or conversation transcript
- [x] Returns content + extraction guidance with existing-node deduplication hints
- [x] Agent-driven (Option B): tool reads, agent extracts and calls `engrama_remember`

### 9b — Adaptive Reflect ✅
- [x] Reflect inspects graph profile before generating queries
- [x] Four new detection patterns: technique transfer, concept clustering, stale knowledge, under-connected nodes
- [x] Dismissed Insights never re-surfaced
- [x] Confidence scoring: path-based, scaled by connection strength and entity count
- [x] Reflect skill and MCP tool both updated

### 9c — Proactivity ✅
- [x] Session state tracks `engrama_remember` calls
- [x] Proactive hint after 10+ entities stored since last reflect
- [x] `engrama_search` surfaces pending Insights related to search query
- [x] Reflect resets proactivity counter

### 9d — Bug fixes ✅
- [x] Proactivity counter moved from lifespan context to module-level `_proactive_state` (cross-call persistence)
- [x] `_run_pattern` supports `any_labels` for OR-logic activation (Problem OR Vulnerability + Course, Project OR Course)
- [x] `training_opportunity` broadened: matches Vulnerability OR Problem
- [x] `shared_technology` broadened: any entity via USES/TEACHES/COMPOSED_OF, activation needs only Technology
- [x] `stale_knowledge` broadened: activates on Project OR Course

### System prompt v0.5 + reference docs ✅
- [x] System prompt slimmed to ~100 lines (token-efficient)
- [x] Detailed content extracted to `docs/reference/` (faceted-classification, query-patterns, node-schema, sync-contract)
- [x] Dual-vault routing (obsidian-mcp vs engrama) added to prompt

## Phase 10 · Additional adapters

- [ ] `adapters/langchain/` — LangChain Memory + Tool
- [ ] `adapters/rest/` — FastAPI HTTP endpoints

## Phase 11 · Vectors (v2)

> DDR-003 Phases A–C complete. Remaining: temporal reasoning, security, multi-scope, benchmarks.

- [x] Protocol-based architecture — `GraphStore`, `VectorStore`, `EmbeddingProvider` (DDR-003 Phase A)
- [x] Local embeddings — `OllamaProvider` with `nomic-embed-text` (DDR-003 Phase B)
- [x] `node_to_text()` — canonical text representation for embedding
- [x] Embedding factory — `create_provider()` reads `.env`, supports `ollama` and `none`
- [x] 27 embedding tests (mocked + live integration)
- [x] `Neo4jVectorStore` with `:Embedded` secondary label strategy (DDR-003 Phase C)
- [x] `HybridSearchEngine` — alpha=0.6 vector / 0.4 fulltext + graph boost (DDR-003 Phase C)
- [x] Embed-on-write in `EngramaEngine.merge_node()` (DDR-003 Phase C)
- [x] `engrama reindex` CLI command (DDR-003 Phase C)
- [x] 18 new tests: vector store, hybrid search, engine embed, factory (DDR-003 Phase C)
- [ ] Temporal reasoning — valid_from/valid_to, confidence decay (DDR-003 Phase D)
- [ ] Security hardening — sanitisation, provenance, trust levels (DDR-003 Phase E)
- [ ] Multi-scope — scope isolation and hierarchy (DDR-003 Phase F)
- [ ] Benchmarks — LOCOMO/LongMemEval harness (DDR-003 Phase G)


## Definition of done

1. Code committed to repo
2. Test passes against real Neo4j (or tmp vault for Obsidian tests)
3. Documented in reference file
4. Conventional commit message

## Test suite status

127 tests expected:

- `test_core.py` — core engine integration tests
- `test_adapters.py` — relate title-key fix (Decision, Problem, name-keyed regression)
- `test_obsidian_sync.py` — adapter, parser, engrama_id injection
- `test_skills.py` — reflect skill (cross-project, shared tech, training, empty graph)
- `test_phase4_skills.py` — remember (4), recall (3), associate (5), forget (7)
- `test_proactive.py` — surface (3), approve/dismiss (5), write to vault (4)
- `test_sdk.py` — SDK public API (14)
- `test_cli.py` — CLI commands via subprocess (6)
- `test_composable.py` — merge logic (9), codegen (3), real files (5), CLI (4)
- `test_embeddings.py` — NullProvider (4), OllamaProvider mocked (10), node_to_text (6), factory (3), live Ollama (5, skip if unavailable)
