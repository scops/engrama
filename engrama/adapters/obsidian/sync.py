"""
engrama/adapters/obsidian/sync.py

Bidirectional sync between Obsidian vault and Neo4j graph.

Contract (DDR-002 — bidirectional sync + vault portability):
  - Every documentable note has a corresponding node in Neo4j.
  - The note carries engrama_id in its YAML frontmatter.
  - engrama_id is the canonical identity link — survives renames and moves.
  - Wiki-links between notes become LINKS_TO relationships in the graph.
  - Frontmatter ``relations`` map is the portable source of typed relations.
  - Deleted notes → node archived (status: "archived"), never hard-deleted.
  - New/modified notes → entities extracted → graph updated.

Sync directions:
  Vault → Graph: ``full_scan()`` merges all frontmatter relations into Neo4j.
  Graph → Vault: ``engrama_relate`` (and MCP tool) writes back to frontmatter.

Usage:
    sync = ObsidianSync(engine, adapter)
    sync.full_scan()          # on startup: reconcile entire vault
    sync.sync_note(path)      # on single note change
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from engrama.core.security import Provenance

from .adapter import ObsidianAdapter
from .parser import NoteParser, ParsedNote

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine

logger = logging.getLogger(__name__)

_SYNC_PROVENANCE = Provenance(source="sync")


class ObsidianSync:
    """Reconciles Obsidian vault notes with Neo4j graph nodes."""

    def __init__(self, engine: EngramaEngine, adapter: ObsidianAdapter) -> None:
        self.engine = engine
        self.adapter = adapter
        self.parser = NoteParser()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def full_scan(self) -> dict:
        """Scan entire vault and reconcile all documentable notes.

        Three-pass strategy:
          1. Parse all notes and merge nodes into Neo4j.
          2. Resolve wiki-links between parsed notes and create LINKS_TO relations.
          3. Merge frontmatter ``relations`` into Neo4j (DDR-002).
             Creates stub nodes for targets that don't exist yet.

        Returns a summary dict with created/updated/skipped/relations/
        frontmatter_relations/stubs_created counts.
        """
        all_notes = self.adapter.list_notes(recursive=True)
        created = updated = skipped = 0

        # Pass 1: create/update all nodes and collect parsed notes
        parsed_notes: list[ParsedNote] = []
        for note_meta in all_notes:
            parsed, status = self._sync_one(note_meta["path"])
            if status == "created":
                created += 1
            elif status == "updated":
                updated += 1
            else:
                skipped += 1
            if parsed is not None:
                parsed_notes.append(parsed)

        # Pass 2: resolve wiki-links → create LINKS_TO relations
        wiki_relations = self._resolve_links(parsed_notes)

        # Pass 3: merge frontmatter relations into Neo4j (DDR-002)
        fm_relations, stubs = self._merge_frontmatter_relations(parsed_notes)

        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "relations": wiki_relations,
            "frontmatter_relations": fm_relations,
            "stubs_created": stubs,
        }

    def sync_note(self, path: str) -> str:
        """Sync a single note to the graph.

        Returns: "created" | "updated" | "skipped"
        """
        parsed, status = self._sync_one(path)

        # If the note has wiki-links, try to resolve them against existing nodes
        if parsed and parsed.wiki_links:
            self._resolve_single_note_links(parsed)

        # Merge frontmatter relations into the graph (DDR-002)
        if parsed and parsed.relations:
            self._merge_single_note_relations(parsed)

        return status

    def delete_notes_for_scope(self, org_id: str, user_id: str, *, apply: bool) -> int:
        """Delete the internal-vault notes belonging to one identity (T030).

        Thin wrapper over :func:`vault_paths_for_scope` +
        :func:`unlink_vault_notes` for the sync/CLI path. The MCP server
        reuses those helpers directly off its async store. See
        :func:`unlink_vault_notes` for the safety contract.
        """
        if self.adapter is None:
            return 0
        paths = vault_paths_for_scope(self.engine._store, org_id, user_id)
        return unlink_vault_notes(self.adapter, paths, apply=apply)

    def archive_missing(self) -> int:
        """Archive graph nodes whose Obsidian notes no longer exist.

        Returns the number of nodes archived.
        """
        records = self.engine._store.list_documented_nodes()
        archived = 0
        for record in records:
            note = self.adapter.read_note(record["path"])
            if not note["success"]:
                self.engine._store.archive_node_for_missing_note(
                    record["label"],
                    record["name"],
                )
                archived += 1
        return archived

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _sync_one(self, path: str) -> tuple[ParsedNote | None, str]:
        """Parse and merge a single note. Returns (parsed_note, status)."""
        note_data = self.adapter.read_note(path)
        if not note_data["success"]:
            return None, "skipped"

        parsed = self.parser.parse(
            path=path,
            content=note_data["content"],
            frontmatter=note_data["frontmatter"],
        )
        if parsed is None:
            return None, "skipped"

        # Ensure engrama_id exists in the note
        if not parsed.engrama_id:
            parsed.engrama_id = str(uuid.uuid4())
            self.adapter.inject_engrama_id(path, parsed.engrama_id)

        # Merge node into Neo4j
        props = {
            **parsed.properties,
            "obsidian_id": parsed.engrama_id,
            "obsidian_path": path,
        }

        result = self.engine.merge_node(parsed.label, props, provenance=_SYNC_PROVENANCE)
        status = "created" if result.get("created") else "updated"
        return parsed, status

    def _resolve_links(self, parsed_notes: list[ParsedNote]) -> int:
        """Resolve wiki-links between parsed notes and create LINKS_TO relations.

        Builds a lookup from filename-stem → node-name, then for each note
        with wiki-links, creates a LINKS_TO relation to the target node.

        Returns the number of relations created.
        """
        # Build lookup: filename stem (lowercase) → (label, name)
        stem_to_node: dict[str, tuple[str, str]] = {}
        for pn in parsed_notes:
            stem = Path(pn.path).stem.lower()
            stem_to_node[stem] = (pn.label, pn.name)
            # Also index by node name (lowercase) for [[Name]] style links
            stem_to_node[pn.name.lower()] = (pn.label, pn.name)

        relations_created = 0
        for pn in parsed_notes:
            for link_target in pn.wiki_links:
                target_key = link_target.strip().lower()
                if target_key in stem_to_node:
                    target_label, target_name = stem_to_node[target_key]
                    if target_name == pn.name:
                        continue  # skip self-links
                    try:
                        self.engine._store.merge_wiki_link(
                            from_label=pn.label,
                            from_name=pn.name,
                            to_label=target_label,
                            to_name=target_name,
                        )
                        relations_created += 1
                    except Exception as e:
                        logger.debug("Could not link %s -> %s: %s", pn.name, target_name, e)

        return relations_created

    def _resolve_single_note_links(self, parsed: ParsedNote) -> int:
        """Resolve wiki-links for a single note against existing graph nodes.

        Used during single-note sync where we don't have all parsed notes
        in memory. Falls back to a graph lookup.
        """
        relations_created = 0
        for link_target in parsed.wiki_links:
            target = link_target.strip()
            if not target:
                continue
            try:
                relations_created += self.engine._store.merge_wiki_link_by_target_name(
                    from_label=parsed.label,
                    from_name=parsed.name,
                    target_name=target,
                )
            except Exception as e:
                logger.debug("Could not link %s -> %s: %s", parsed.name, target, e)
        return relations_created

    # ------------------------------------------------------------------
    # DDR-002: frontmatter relation sync (vault → graph)
    # ------------------------------------------------------------------

    def _merge_frontmatter_relations(self, parsed_notes: list[ParsedNote]) -> tuple[int, int]:
        """Merge all frontmatter ``relations`` into Neo4j.

        For each note with a ``relations`` map in its frontmatter, creates
        the corresponding typed relationships in the graph.  If a target
        node does not exist, creates a stub node (name only, status: "stub").

        Returns (relations_merged, stubs_created).
        """
        # Build a lookup of known nodes for label inference
        known_nodes: dict[str, str] = {}  # lowercase name → label
        for pn in parsed_notes:
            known_nodes[pn.name.lower()] = pn.label

        relations_merged = 0
        stubs_created = 0

        for pn in parsed_notes:
            if not pn.relations:
                continue
            for rel_type, targets in pn.relations.items():
                for target_name in targets:
                    target_lower = target_name.lower()

                    # Try to find the target node's label
                    if target_lower in known_nodes:
                        target_label = known_nodes[target_lower]
                    else:
                        # Look up in graph
                        target_label = self._find_node_label(target_name)

                    if target_label is None:
                        # Create stub node (DDR-002: stub creation)
                        target_label = self._infer_stub_label(rel_type)
                        try:
                            self.engine.merge_node(
                                target_label,
                                {
                                    "name": target_name,
                                    "status": "stub",
                                },
                                provenance=_SYNC_PROVENANCE,
                            )
                            stubs_created += 1
                            known_nodes[target_lower] = target_label
                            logger.info(
                                "Created stub %s:%s (target of %s from %s)",
                                target_label,
                                target_name,
                                rel_type,
                                pn.name,
                            )
                        except Exception as e:
                            logger.warning(
                                "Could not create stub for %s: %s",
                                target_name,
                                e,
                            )
                            continue

                    # Merge the relation
                    try:
                        self.engine.merge_relation(
                            from_name=pn.name,
                            from_label=pn.label,
                            rel_type=rel_type,
                            to_name=target_name,
                            to_label=target_label,
                        )
                        relations_merged += 1
                    except Exception as e:
                        logger.debug(
                            "Could not merge relation %s -[%s]-> %s: %s",
                            pn.name,
                            rel_type,
                            target_name,
                            e,
                        )

        return relations_merged, stubs_created

    def _merge_single_note_relations(self, parsed: ParsedNote) -> int:
        """Merge frontmatter relations for a single note into the graph.

        Used during single-note sync.  Creates stub nodes for missing targets.
        Returns the number of relations merged.
        """
        merged = 0
        for rel_type, targets in parsed.relations.items():
            for target_name in targets:
                target_label = self._find_node_label(target_name)

                if target_label is None:
                    target_label = self._infer_stub_label(rel_type)
                    try:
                        self.engine.merge_node(
                            target_label,
                            {
                                "name": target_name,
                                "status": "stub",
                            },
                            provenance=_SYNC_PROVENANCE,
                        )
                        logger.info(
                            "Created stub %s:%s (target of %s from %s)",
                            target_label,
                            target_name,
                            rel_type,
                            parsed.name,
                        )
                    except Exception as e:
                        logger.warning(
                            "Could not create stub for %s: %s",
                            target_name,
                            e,
                        )
                        continue

                try:
                    self.engine.merge_relation(
                        from_name=parsed.name,
                        from_label=parsed.label,
                        rel_type=rel_type,
                        to_name=target_name,
                        to_label=target_label,
                    )
                    merged += 1
                except Exception as e:
                    logger.debug(
                        "Could not merge relation %s -[%s]-> %s: %s",
                        parsed.name,
                        rel_type,
                        target_name,
                        e,
                    )
        return merged

    def _find_node_label(self, name: str) -> str | None:
        """Look up a node's label in the graph by name (case-insensitive),
        restricted to the engine's default scope.

        Returns the first label found, or None if the node does not exist
        within the caller's scope.
        """
        try:
            return self.engine._store.lookup_node_label(name, scope=self.engine.default_scope)
        except Exception:
            return None

    @staticmethod
    def _infer_stub_label(rel_type: str) -> str:
        """Infer a reasonable label for a stub node based on the relationship type.

        Falls back to "Concept" for unknown relationship types — Concept is
        the universal bridge node in the faceted classification system.
        """
        _REL_TO_LABEL: dict[str, str] = {
            "INSTANCE_OF": "Concept",
            "COMPOSED_OF": "Technology",
            "PERFORMS": "Concept",
            "SOLVED_BY": "Decision",
            "SERVES": "Concept",
            "BELONGS_TO": "Project",
            "IN_DOMAIN": "Domain",
            "USES": "Technology",
            "COVERS": "Concept",
            "TEACHES": "Course",
            "FOR": "Client",
            "INCLUDES": "Exercise",
            "HAS_MATERIAL": "Material",
            "EXPLOITS": "Vulnerability",
            "EXECUTED_WITH": "Tool",
            "TARGETS": "Target",
            "REQUIRES": "Technology",
            "TRAINS_ON": "Dataset",
            "RUNS": "Model",
            "EVALUATES": "Model",
            "FEEDS": "Pipeline",
        }
        return _REL_TO_LABEL.get(rel_type, "Concept")


# ---------------------------------------------------------------------------
# Spec 001 US-3 / T030 — vault note erasure by identity
# ---------------------------------------------------------------------------
#
# Module-level so both the sync path (:meth:`ObsidianSync.delete_notes_for_scope`)
# and the async MCP server (off its async store) share one implementation. The
# scope→paths query is backend-aware; the unlink is backend-agnostic and is the
# single place the vault-boundary (P6) guard lives.


def vault_paths_for_scope(store: object, org_id: str, user_id: str) -> list[str]:
    """Vault-relative paths of in-scope nodes carrying an ``obsidian_path``.

    Backend-aware over a *sync* store: SQLite via ``_conn``, Neo4j via
    ``_client``. Several nodes may point at the same note, so paths are
    de-duplicated — the caller counts notes, not node rows. Must be called
    *before* the graph rows are erased, otherwise the paths are gone.
    """
    conn = getattr(store, "_conn", None)
    if conn is not None:
        rows = conn.execute(
            "SELECT DISTINCT json_extract(props, '$.obsidian_path') AS p FROM nodes "
            "WHERE json_extract(props, '$.org_id') = ? "
            "AND json_extract(props, '$.user_id') = ? "
            "AND json_extract(props, '$.obsidian_path') IS NOT NULL",
            (org_id, user_id),
        ).fetchall()
        return [r["p"] for r in rows if r["p"]]
    client = getattr(store, "_client", None)
    if client is not None:
        rows = client.run(
            "MATCH (n {org_id: $org, user_id: $user}) "
            "WHERE n.obsidian_path IS NOT NULL "
            "RETURN DISTINCT n.obsidian_path AS p",
            {"org": org_id, "user": user_id},
        )
        return [r["p"] for r in rows if r["p"]]
    return []


def unlink_vault_notes(
    adapter: ObsidianAdapter | None, rel_paths: list[str], *, apply: bool
) -> int:
    """Delete (``apply=True``) or count (``apply=False``) vault notes.

    Returns the number of notes that exist inside the internal vault and
    belong to the identity. Safety:

    * No-op (returns ``0``) when no adapter / vault is configured — the
      gateway and the single-user MVP may run vault-less.
    * Only files that resolve *inside* ``VAULT_PATH`` are touched. A stored
      ``obsidian_path`` that would escape the vault root (P6: never external
      vaults) is skipped, not followed.
    """
    if adapter is None:
        return 0
    count = 0
    for rel in rel_paths:
        try:
            target = adapter._resolve(rel)
        except ValueError:
            # Path traversal / outside the vault root — never follow it.
            continue
        if not target.exists():
            continue
        count += 1
        if apply:
            target.unlink()
    return count
