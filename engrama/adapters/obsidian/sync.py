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

from .adapter import ObsidianAdapter
from .parser import NoteParser, ParsedNote

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine

logger = logging.getLogger(__name__)


class ObsidianSync:
    """Reconciles Obsidian vault notes with Neo4j graph nodes."""

    def __init__(self, engine: "EngramaEngine", adapter: ObsidianAdapter) -> None:
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

    def archive_missing(self) -> int:
        """Archive graph nodes whose Obsidian notes no longer exist.

        Returns the number of nodes archived.
        """
        records = self.engine.run(
            "MATCH (n) WHERE n.obsidian_path IS NOT NULL "
            "RETURN labels(n)[0] AS label, n.name AS name, n.obsidian_path AS path",
            {},
        )
        archived = 0
        for record in records:
            note = self.adapter.read_note(record["path"])
            if not note["success"]:
                self.engine.run(
                    "MATCH (n {name: $name}) WHERE $label IN labels(n) "
                    "SET n.status = 'archived', n.updated_at = datetime()",
                    {"name": record["name"], "label": record["label"]},
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

        result = self.engine.merge_node(parsed.label, props)
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
                        self.engine.run(
                            f"MATCH (a {{name: $from_name}}) "
                            f"WHERE $from_label IN labels(a) "
                            f"MATCH (b {{name: $to_name}}) "
                            f"WHERE $to_label IN labels(b) "
                            f"MERGE (a)-[:LINKS_TO]->(b)",
                            {
                                "from_name": pn.name,
                                "from_label": pn.label,
                                "to_name": target_name,
                                "to_label": target_label,
                            },
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
                # Try to find target node by name (case-insensitive)
                result = self.engine.run(
                    "MATCH (b) WHERE toLower(b.name) = toLower($target) "
                    "WITH b LIMIT 1 "
                    "MATCH (a {name: $from_name}) WHERE $from_label IN labels(a) "
                    "MERGE (a)-[:LINKS_TO]->(b)",
                    {
                        "target": target,
                        "from_name": parsed.name,
                        "from_label": parsed.label,
                    },
                )
                relations_created += 1
            except Exception as e:
                logger.debug("Could not link %s -> %s: %s", parsed.name, target, e)
        return relations_created

    # ------------------------------------------------------------------
    # DDR-002: frontmatter relation sync (vault → graph)
    # ------------------------------------------------------------------

    def _merge_frontmatter_relations(
        self, parsed_notes: list[ParsedNote]
    ) -> tuple[int, int]:
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
                            self.engine.merge_node(target_label, {
                                "name": target_name,
                                "status": "stub",
                            })
                            stubs_created += 1
                            known_nodes[target_lower] = target_label
                            logger.info(
                                "Created stub %s:%s (target of %s from %s)",
                                target_label, target_name, rel_type, pn.name,
                            )
                        except Exception as e:
                            logger.warning(
                                "Could not create stub for %s: %s",
                                target_name, e,
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
                            pn.name, rel_type, target_name, e,
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
                        self.engine.merge_node(target_label, {
                            "name": target_name,
                            "status": "stub",
                        })
                        logger.info(
                            "Created stub %s:%s (target of %s from %s)",
                            target_label, target_name, rel_type, parsed.name,
                        )
                    except Exception as e:
                        logger.warning(
                            "Could not create stub for %s: %s",
                            target_name, e,
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
                        parsed.name, rel_type, target_name, e,
                    )
        return merged

    def _find_node_label(self, name: str) -> str | None:
        """Look up a node's label in the graph by name (case-insensitive).

        Returns the first label found, or None if the node does not exist.
        """
        try:
            records = self.engine.run(
                "MATCH (n) WHERE toLower(n.name) = toLower($name) "
                "RETURN labels(n)[0] AS label LIMIT 1",
                {"name": name},
            )
            if records:
                return records[0]["label"]
        except Exception:
            pass
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
