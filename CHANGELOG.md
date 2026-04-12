# Changelog

All notable changes to Engrama will be documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/)

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
