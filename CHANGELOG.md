# Changelog

All notable changes to Engrama will be documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/)

---

## [Unreleased]

### Fixed
- **Engine `merge_node` honours `TITLE_KEYED_LABELS` regardless of the
  caller's property bag.** Previously the engine picked the merge key
  from whichever of `name` or `title` was present in `properties`, so a
  caller that put `name` in the bag for a title-keyed label (notably
  the MCP `engrama_remember` tool, which forwards `params.properties`
  verbatim) bypassed the schema convention. The result was duplicate
  rows on Neo4j and silent property-key divergence on SQLite between
  SDK and MCP writes of the same logical node. The engine now
  canonicalises the merge key after sanitisation; the non-canonical
  alias is silently dropped (matching the sanitiser's behaviour with
  reserved keys). Existing duplicate rows from earlier writes are not
  healed — a one-shot `engrama migrate keys` command is tracked as a
  follow-up. (#51)
- **`Insight` added to `TITLE_KEYED_LABELS`.** The auto-injected
  `Insight` dataclass uses `title` as its merge key (Neo4j constraint
  enforces `n.title IS UNIQUE`, every `reflect`/`proactive` query
  filters by `title`), but the codegen only added user-defined nodes
  to `TITLE_KEYED_LABELS`, so `Insight` was missing. The engine fix
  above exposed the mismatch by canonicalising `title → name` for any
  label not in the set, breaking every Cypher `MATCH (i:Insight
  {title: ...})`. Patched both `engrama/core/schema.py` and the
  generator scripts so future regenerations include `Insight`.

---

## [0.10.0] — 2026-05-14

CI maturity + supply-chain hardening for the first public PyPI publish.
The library APIs are unchanged from 0.9.0; this release is about making
engrama installable, auditable, and migratable for users outside the
dev environment.

### Added
- **`engrama export` / `engrama import`** — backend-agnostic NDJSON dump
  and restore for the graph + vectors, enabling cross-backend migrations
  (SQLite ↔ Neo4j) as a first-class CLI path. Format is one JSON object
  per line: envelope, then `node` / `relation` / `vector` records.
  Vectors only restore when source and target embedding dimensions
  match; mismatches are reported and `engrama reindex` rebuilds them
  under the active embedder. (#30)
- **Release pipeline** (`.github/workflows/release.yml`) — six-stage
  `guardian → build → sbom → attest → publish → release-notes` triggered
  by `v*` tags. PyPI **trusted publishing** (OIDC, no API key in
  secrets), CycloneDX + SPDX SBOMs attached to the GitHub Release, SLSA
  build-provenance attestations on the wheel and sdist, and a
  `workflow_dispatch` dry-run path. Guardian fails fast on version
  drift between `pyproject.toml`, `engrama/__init__.py` and the topmost
  CHANGELOG entry. (#27)
- **PR-level vulnerability gate** — `audit-deps` job runs `pip-audit`
  on PRs that touch `pyproject.toml` or `uv.lock` (always on push to
  `main`). Blocks on CVSS ≥ 7.0 (looked up from OSV) or any advisory
  with an upstream fix; warns on LOW/MEDIUM with no fix. (#27)
- **CI matrix** across Python 3.11 / 3.12 / 3.13 for `import-smoke` and
  `test-sqlite`. New `test-neo4j` integration job uses a
  `neo4j:5.26.4-community` service container with the committed
  `scripts/init-schema.cypher` applied via the Python driver. (#26)
- **Phase-1 CI baseline** — `lint` (ruff format + check), `test-sqlite`
  (SQLite-only suite, no Docker), `import-smoke` (DDR-004 promise gate).
  Dependabot weekly for `pip` and `github-actions`, monthly for
  `docker`. (#7–#12)
- **Phase-4 repo hygiene** — `.github/CODEOWNERS`,
  `pull_request_template.md`, structured issue templates, `SECURITY.md`
  (disclosure via GitHub private advisories), `lychee-action` weekly +
  per-PR link checker, `DDR-template.md`. (#22)

### Changed
- **README embedder section** — expanded to a six-provider matrix
  (Ollama, OpenAI, LM Studio, vLLM, llama.cpp, Jina) with
  copy-pasteable `.env` blocks, recommended models + dimensions, start
  commands where relevant, and provider-specific gotchas. Mirrored in
  `README_ES.md`. (#29)
- **Misconfig surfacing in the CLI/MCP** — `GRAPH_BACKEND=neo4j`
  without the `[neo4j]` extra (#23) and `engrama-mcp` without the
  `[mcp]` extra (#28) now both emit a single-line install hint to
  stderr and exit 1, replacing the prior raw Python tracebacks.
- **Documentation** — install instructions aligned with source-only
  reality, Codex + ChatGPT Desktop MCP setup snippets added in both
  READMEs. (#15, #23, #25)
- **License metadata** — `pyproject.toml` corrected to `Apache-2.0`.

### Fixed
- **Base install no longer eagerly imports `neo4j`** —
  `engrama/core/client.py` defers the import to
  `EngramaClient.__init__`, so `import engrama` works on a
  `pip install engrama` base install with no extras (DDR-004 promise
  gate). (#11)
- **FTS5 MATCH sanitization on SQLite** — hyphenated queries like
  `engrama-mcp-server` are wrapped as phrases instead of being treated
  as FTS5 grammar, restoring the fulltext path. 14 tests added. (#16)
- **Hybrid search degraded-mode signal** — when the embeddings provider
  is unreachable, the search engine exposes
  `last_mode = {mode, degraded, reason}` and the MCP `engrama_search`
  response carries a `search_mode` field, so callers can distinguish a
  fulltext-only fallback from a healthy hybrid result. (#20)
- **Degenerate embeddings caught at write time** —
  `engrama/embeddings/health.is_degenerate_vector` flags
  `needs_reindex=true` on the node and skips vector storage;
  `list_nodes_for_embedding(force=False)` now pulls those nodes back so
  `engrama reindex` heals them. (#21)
- **`under_connected` reflect pattern excludes stub neighbours** — both
  backends now filter out `status='stub'` nodes when counting
  connections. (#19)
- **Ruff import grouping** repo-wide cleanup. (#24)

---

## [0.9.0] — 2026-05-10

Portable storage — SQLite + sqlite-vec as the default backend, Neo4j moved
to an opt-in extra, single OpenAI-compatible embedder, full async contract
parity (DDR-004, PR #5).

### Added
- **SQLite backend** (`engrama/backends/sqlite/`) — full implementation of
  the `GraphStore` and `VectorStore` protocols on top of `sqlite3` and the
  `sqlite-vec` extension. Modules: `store.py` (sync graph store, 36+
  methods, FTS5 fulltext), `async_store.py` (async wrapper that mirrors
  `Neo4jAsyncStore`'s rich return shapes), `vector.py` (`SqliteVecStore`
  using the `vec0` virtual table), `schema.sql` (auto-applied on first
  connect). Default DB path: `~/.engrama/engrama.db` (override via
  `ENGRAMA_DB_PATH`).
- **OpenAI-compatible embedding provider** (`engrama/embeddings/openai_compat.py`)
  — single client speaking the OpenAI `/v1/embeddings` shape, drives
  Ollama, OpenAI proper, LM Studio, vLLM, llama.cpp, Jina, and any
  future compatible service. Sync (`embed`, `embed_batch`) and async
  (`aembed`, `aembed_batch`) methods.
- **Backend factory** (`engrama/backends/__init__.py`) — `create_stores()`
  and `create_async_stores()` dispatch the engine, CLI, SDK, and MCP
  server through a single entry point. `GRAPH_BACKEND` env var (or
  explicit config dict) selects the implementation. Skills and tools no
  longer hardcode any backend.
- **`engrama-mcp` CLI flags** — `--backend {sqlite,neo4j}` (default
  `sqlite`), `--db-path`, `--neo4j-uri`, `--neo4j-password`,
  `--neo4j-database`, `--vault-path`. Defaults read from environment.
- **Async GraphStore contract suite** (`tests/contracts/test_async_graphstore_contract.py`)
  — parameterised over `sqlite-async` and `neo4j-async`. Pins the rich
  response shapes the MCP server depends on (`merge_node` returns
  `{"node": ..., "created": ...}`, neighbours come back as `{label,
  name, via, properties}`, etc.). 12 tests × 2 backends.
- **`get_approved_titles`** on every store layer (Neo4j async, SQLite
  async, SQLite sync) — used by reflect to skip patterns the user has
  already approved (see Fixed below).
- **76 SQLite-only tests** (`tests/backends/test_sqlite*.py`,
  `tests/contracts/test_graphstore_contract.py` SQLite branch) — pass on
  a fresh checkout with no `.env` and no Docker. CI runs them by default.
- **BACKENDS.md** — newcomer-facing decision guide: when to pick SQLite,
  when to pick Neo4j, how to switch, FAQ. Linked from README and
  ARCHITECTURE.
- **DDR-004** — formal record of the portable storage decision, including
  the three regressions found and fixed during e2e testing.

### Changed
- **Default `GRAPH_BACKEND` is now `sqlite`** (was `neo4j`). Existing
  installs that rely on Neo4j must set `GRAPH_BACKEND=neo4j` explicitly.
- **`neo4j` driver moves to an opt-in extra** (`uv sync --extra
  neo4j`, or the `engrama[neo4j]` extra once Engrama ships on PyPI).
  Base install ships with `sqlite-vec`, `httpx`, `pydantic`,
  `python-dotenv`, `pyyaml` only.
- **`Neo4jGraphStore` returns plain dicts** at the boundary (Phase 1 of
  the spec). `EngramaEngine`, `recall.py` and other internal callers
  consume Python `dict` rather than driver-specific types.
- **`HybridSearchEngine`** copies enrichment fields (`summary`, `tags`,
  `confidence`, `updated_at`) onto `SearchResult` for vector-only hits.
  Both backends' `search_similar` now project these fields.
- **`engrama init`** is a no-op on SQLite for Cypher schema statements
  (the schema is in `backends/sqlite/schema.sql`, applied automatically)
  but still seeds domain nodes from the active profile.
- **Documentation overhaul** — README, README_ES, ARCHITECTURE, ROADMAP,
  VISION, GRAPH-SCHEMA, CONTRIBUTING and GLOSARIO_ES all updated to
  reflect the dual-backend reality. `.env.example` now defaults to the
  zero-dep SQLite path with Neo4j commented as opt-in.

### Fixed
- **Async store contract drift on SQLite.** `SqliteAsyncStore` was
  forwarding sync calls via `__getattr__`, leaking the legacy
  `[{"n": ...}]` shape to the MCP server which expected the rich
  `Neo4jAsyncStore`-style `{"node": ..., "created": ...}`. Crashed any
  `engrama_remember` call against the SQLite backend. Replaced with
  explicit method-by-method delegation that translates shapes; locked
  in by the new async contract suite.
- **Reflect overwriting approved Insights.** Each `_build_*` helper in
  `engrama_reflect` called `merge_node` with `status="pending"`,
  silently undoing user approvals on re-runs. Reflect now filters
  candidates against `dismissed | approved`. The output payload also
  exposes `approved_count` next to `dismissed_count`.
- **Search dropping enrichment on pure-semantic hits.** The hybrid
  scorer only copied `summary`/`tags` from fulltext results, so nodes
  ranked solely by vector similarity surfaced with empty fields.
  `search_similar` now projects the enrichment fields and the scorer
  copies them on the vector path.

### Removed
- Implicit assumption that Neo4j is required to use Engrama. Neo4j is
  still fully supported via the `neo4j` extra; nothing about its
  feature surface has changed.

---

## [0.8.0] — 2026-04-14

Temporal reasoning — confidence decay, fact supersession, and time-travel queries (DDR-003 Phase D).

### Added
- **Confidence decay**: `decay_confidence()` in `Neo4jAsyncStore` applies exponential decay (`confidence × exp(-rate × days_old)`) to stale nodes. Supports dry-run mode, label filtering, and auto-archival of nodes below a confidence threshold. Sync equivalent was already in `Neo4jGraphStore.decay_scores()`.
- **`valid_to` support**: `merge_node()` now accepts `valid_to` to mark facts as superseded. Setting `valid_to` auto-halves confidence. Updating a superseded node clears `valid_to` (revival) and returns a conflict warning.
- **Temporal queries**: `query_at_date(date, label?)` in both async and sync stores — returns nodes valid at a specific date (`valid_from <= date AND (valid_to IS NULL OR valid_to >= date)`).
- **Enhanced CLI `engrama decay`**: `--dry-run` now shows a sample table of nodes that would be affected with current vs projected confidence values.
- **Enhanced stale knowledge detection**: `reflect` stale_knowledge pattern now also considers nodes with `confidence < 0.3` as stale regardless of age. Insight body includes confidence value for severity assessment.
- **Async store tests**: `TestAsyncDecayConfidence` (5 tests), `TestAsyncValidTo` (3 tests), `TestAsyncQueryAtDate` (3 tests) added to `test_temporal.py`.

### Changed
- ARCHITECTURE.md: rewritten 5-layer diagram (Adapters → Skills → Engine → Protocols → Backends), added temporal reasoning section, configuration reference table, updated directory structure to match reality (15 test files).
- GRAPH-SCHEMA.md: added temporal fields (`valid_to`, `decayed_at`, `embedding`) to all-nodes section.
- `temporal.py`: `days_since()` now handles Neo4j `DateTime` objects via `.to_native()`.
- `search.py`: min-max normalization returns `1.0` for single-result sets (was `0.0`).

### Fixed
- Search normalization bug: a single search result normalized to score 0.0 instead of 1.0.
- Neo4j `DateTime` incompatibility in `days_since()`: `TypeError` when subtracting `neo4j.time.DateTime` from `datetime.datetime`.
- `valid_to` and caller-supplied `valid_from` now stored as Neo4j `datetime()` instead of raw strings, fixing `query_at_date` comparisons.

---

## [0.7.0] — 2026-04-13

Async embedding providers and hybrid search (DDR-003 Phase B + C).

### Added
- **Async embedding methods**: `OllamaProvider` now has `aembed()`, `aembed_batch()`, `ahealth_check()`, `aclose()` using `httpx.AsyncClient`. Sync methods (urllib) remain for CLI/SDK backward compatibility. `NullProvider` also has async counterparts.
- **Async hybrid search**: `HybridSearchEngine.asearch()` — async counterpart of `search()`. Uses `aembed()` and async store methods. Deployed in MCP server for non-blocking search.
- **Embed-on-write**: `engrama_remember` and `engrama_sync_note` MCP tools now embed nodes automatically when `EMBEDDING_PROVIDER` is configured. Uses async `aembed()` to avoid blocking the event loop.
- `core/text.py` — re-export of `embeddings/text.py` for import convenience.
- `httpx>=0.27` added as optional dependency (`embeddings` and `mcp` extras).
- **Test suite**: `test_hybrid_search.py` (unit tests with mock stores for sync+async search, scoring formula, graceful degradation, integration tests with real Neo4j+Ollama). Async embedding tests added to `test_embeddings.py` (NullProvider + OllamaProvider async methods).

### Changed
- **MCP server lifespan**: simplified — no longer creates redundant sync stores for hybrid search. The async store serves as both `GraphStore` and `VectorStore` for `HybridSearchEngine.asearch()`.
- **MCP `engrama_search`**: uses `HybridSearchEngine.asearch()` with the async store directly, eliminating the sync→async impedance mismatch.
- **MCP embed-on-write**: uses `await embedder.aembed()` instead of sync `embedder.embed()`, preventing event loop blocking.
- ARCHITECTURE.md: added "Embedding and hybrid search" section documenting dual-mode providers, embed-on-write, vector index strategy, and scoring formula.

### Fixed
- **Event loop blocking in MCP server**: embedding and hybrid search previously called sync methods from async context, blocking the event loop. Now fully async.

---

## [0.6.0] — 2026-04-13

Protocol extraction and bug fixes (DDR-003 Phase A). Zero Cypher in server.py.

### Added
- **DDR-003 Phase A — Protocol layer**: abstract `GraphStore`, `VectorStore`, `EmbeddingProvider` protocols in `core/protocols.py`. All storage operations route through backend implementations — no adapter, skill, or tool writes Cypher directly.
- `Neo4jAsyncStore` (`backends/neo4j/async_store.py`) — async backend for the MCP server. Contains **all** Cypher that was previously inline in `server.py`. Methods: `merge_node`, `get_node`, `delete_node`, `merge_relation`, `get_neighbours`, `get_node_with_neighbours`, `fulltext_search`, `count_labels`, `run_pattern`, `lookup_node_label`, plus vector ops (`store_embedding`, `search_similar`, `delete_embedding`, `count_embeddings`) and Insight ops (`get_dismissed_titles`, `get_pending_insights`, `get_insight_by_title`, `update_insight_status`, `mark_insight_synced`, `find_insight_by_source_query`, `list_existing_nodes`).
- `create_async_store()` factory in `backends/__init__.py` — reads `EMBEDDING_DIMENSIONS` from config/env, returns configured `Neo4jAsyncStore`.
- `count_labels()` and `close()` methods on sync `Neo4jGraphStore`.
- `NullGraphStore` and `NullVectorStore` — no-op implementations for testing and dry-run mode.
- **Test suites**: `test_protocols.py` (protocol conformance for all stores, NullGraphStore/NullVectorStore behaviour, async store method inventory) and `test_neo4j_store.py` (integration tests against real Neo4j: merge, dedup, update, relations, fulltext COALESCE, neighbours, count_labels, run_cypher, get/delete node, health_check).
- `.env.example`: `HYBRID_ALPHA` (fulltext vs vector weight) and `HYBRID_GRAPH_BETA` (graph topology boost).

### Fixed
- **BUG-006**: `engrama_search` returned `null` names for title-keyed nodes (Decision, Problem, Vulnerability, etc.). Fulltext search and neighbour queries now use `COALESCE(node.name, node.title)`.
- **BUG-007**: `engrama_reflect` generated duplicate under-connected Insights on repeated runs. `_detect_under_connected` now checks for existing pending/approved Insight by `source_query` before creating; updates in place if found; respects previously dismissed Insights.
- **BUG-008**: `engrama_context` showed duplicate relation entries in the `via` array. Deduplicated in `get_node_with_neighbours`.

### Changed
- **server.py**: rewired from inline Cypher to `Neo4jAsyncStore` method calls. Contains **zero** Cypher strings (was ~2053 lines, now ~1753). MCP tools handle orchestration, validation, vault I/O, and response formatting only.
- **server.py lifespan**: creates `Neo4jAsyncStore` via `create_async_store(driver, database, config)` and stores it in context alongside the raw driver.
- ARCHITECTURE.md: updated stack table, directory structure, and added "Protocol layer (DDR-003 Phase A)" section documenting sync/async stores, null implementations, and factory pattern.

---

## [0.5.0] — 2026-04-12

Three core features that make Engrama valuable beyond a raw Neo4j wrapper. System prompt v0.5.

### Added
- **Phase 1 — Ingestion**: `engrama_ingest` MCP tool. Reads a vault note, raw text, or conversation transcript and returns content with entity extraction guidance. The agent extracts entities and calls `engrama_remember` for each one. Includes graph deduplication hints (existing nodes listed in response).
- **Phase 2 — Adaptive Reflect**: `engrama_reflect` now inspects the graph before querying. Selects only applicable patterns based on what labels have data. Four new detection patterns: technique transfer (cross-domain technique applicability), concept clustering (3+ entities sharing a Concept), stale knowledge (90+ day old nodes linked to active Projects), under-connected nodes (<2 relationships). Previously dismissed Insights are never re-surfaced. Confidence scoring based on connection strength and entity count.
- **Phase 3 — Proactivity**: Session state tracks `engrama_remember` calls. After 10+ entities stored since last reflect, `engrama_remember` returns a `proactive_hint` suggesting the agent run reflect. `engrama_search` checks for pending Insights related to the search query and surfaces them inline. `engrama_reflect` resets the counter.
- **Reference docs**: Extracted v0.4 detailed content into `docs/reference/` (faceted-classification, query-patterns, node-schema, sync-contract). System prompt v0.5 is lean; reference docs are the "workshop manual".
- **DDR-001**: Design decision record for the faceted classification system.

### Fixed
- **Phase 3 proactivity counter not firing**: `_proactive_state` moved from FastMCP lifespan context (not reliably mutable across tool calls) to a module-level dict. Counter now persists correctly across `engrama_remember` invocations.
- **training_opportunity never activating**: query only matched `Problem {status: "open"}` but real graphs have `Vulnerability` nodes (status: "demonstrated"). Broadened WHERE clause: `(issue:Vulnerability) OR (issue:Problem AND issue.status = $open_status)`.
- **shared_technology skipped in most graphs**: required both `Project` AND `Technology` labels, but many graphs have Courses or Decisions sharing technologies. Broadened: matches any entity via `USES`/`TEACHES`/`COMPOSED_OF`, activation requires only `Technology` label.
- **stale_knowledge skipped when only Courses exist**: activation required `Project` but the query also checks `Course` connections. Broadened: activates when either `Project` OR `Course` exists.
- **`_run_pattern` too rigid for OR-logic**: added `any_labels` parameter — each entry is an OR-group where at least one label must have data. Used by `training_opportunity` (Problem OR Vulnerability + Course) and `stale_knowledge` (Project OR Course).

### Changed
- System prompt v0.5: shorter, token-efficient. Adds dual-vault routing (obsidian-mcp vs engrama). References `docs/reference/` for details.
- `engrama_search` response now wraps results in `{"results": [...]}` object (was bare array) to accommodate optional `pending_insights` and `proactive_hint` fields.
- Reflect skill confidence scores adjusted: cross-project 0.85, shared-tech 0.7, training 0.65, technique-transfer 0.5–0.9 (scaled by related entities), concept-clustering 0.5–0.9 (scaled by count), stale 0.5, under-connected 0.4.

---

## [0.4.0] — 2026-04-12

Bug-fix sprint + schema expansion. System prompt v0.4.

### Fixed
- **BUG-001**: CLI `init` dropped fulltext index — comment lines inside `;`-split chunks caused the entire CREATE FULLTEXT INDEX statement to be silently discarded.
- **BUG-002**: `engrama_remember` never created vault notes — nodes had no `obsidian_path`, breaking the DDR-002 contract. Now creates full YAML frontmatter with engrama_id, type, properties, and empty relations block.
- **BUG-003**: `engrama_relate` failed to write to vault because `obsidian_path` was always null (cascading from BUG-002). Added fallback: if source node has no vault note, create one on-the-fly.
- **BUG-005**: `engrama_remember` crashed when `relations` dict was passed inside `properties` — Neo4j rejects Map values as node properties. Now extracts relations before MERGE and merges both input paths (top-level field and nested in properties) into a single processing loop.

### Added
- **BUG-004**: Domain seed data for all modules — `engrama init` now seeds domain nodes and key concepts for hacking, teaching, photography, and AI modules.
- **BUG-005**: Inline relations in `engrama_remember` — pass `relations: {TEACHES: [Python]}` and targets are found/created (with stub creation) + relationships merged + vault frontmatter written, all in one call.
- **FIX-008**: `Material` node type for teaching artifacts (cheatsheets, slides, exercises, reference cards). Properties: name, type, format, status, notes. New relation: `HAS_MATERIAL` (Course → Material).
- **DDR-001**: Design decision record for the faceted classification system (was referenced but missing).

### Changed
- **FIX-006**: System prompt section 4 — relaxed "immediately call relate" to reflect that `remember` now supports inline relations.
- **FIX-007**: System prompt section 3 — `INSTANCE_OF` is now mandatory only for Problem, Decision, Vulnerability. Recommended for all other types when it adds discovery value.
- System prompt version bumped to 0.4.0. File renamed from `v0.3` to `v0.4`.

### Removed
- `_obsidian_mcp_ref/` — development reference folder, not part of the codebase.
- `.claude/` — session working directory artifact.

---

## [0.3.0] — 2026-04-12

Bidirectional sync and vault portability (DDR-002).

### Added
- **DDR-002**: Bidirectional sync — all graph relations are serialized into each note's YAML frontmatter `relations` map. Vault and graph are co-equal sources of truth.
- `ObsidianAdapter.add_relation()`, `remove_relation()`, `set_relations()` — idempotent frontmatter relation management.
- `NoteParser` extracts `relations` from frontmatter, normalises scalars to lists, uppercases relation types.
- `ObsidianSync.full_scan()` three-pass strategy: nodes → wiki-links → frontmatter relations.
- `_infer_stub_label()` — maps relation types to likely target labels for stub node creation.
- `AssociateSkill` writes relations to vault frontmatter (dual-write contract).
- DDR-002 test suite: `TestParserRelations` (3 tests), `TestAdapterRelations` (9 tests).

---

## [0.2.0] — 2026-04-11

Faceted classification system (DDR-001).

### Added
- **DDR-001**: Six-facet classification adapted from Ranganathan's PMEST + BFO. Facets: identity, composition, action, purpose, context, domain.
- Composable profiles: `base.yaml` + domain modules (hacking, teaching, photography, ai).
- `generate_from_profile.py` — merges profiles, deduplicates relations, generates `schema.py` and `init-schema.cypher`.
- CLI: `engrama init --profile base --modules hacking teaching photography ai`.
- System prompt v0.2 with full faceted classification documentation.

---

## [0.1.0] — 2026-04-10

Initial release. Phases 0–7: core engine, MCP adapter, Obsidian sync, skills, reflect, proactive insights, SDK, CLI.

### Added
- Neo4j 5.26 LTS with Docker Compose setup.
- Core engine: MERGE semantics, timestamps, fulltext search.
- FastMCP server with 10 tools: search, remember, relate, context, sync_note, sync_vault, reflect, surface_insights, approve_insight, write_insight_to_vault.
- Obsidian adapter: vault ↔ graph sync via engrama_id.
- Four skill classes: RememberSkill, RecallSkill, AssociateSkill, ForgetSkill.
- ReflectSkill: cross-project solution transfer, shared technology detection, training opportunity discovery.
- ProactiveSkill: surface/approve/dismiss/write Insights to vault.
- Python SDK: `Engrama` class wrapping all skills.
- CLI: `engrama init`, `engrama verify`, `engrama reflect`, `engrama search`.
- 100 integration tests across 9 test files.

---
