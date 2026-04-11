"""
engrama/adapters/obsidian/sync.py

Bidirectional sync between Obsidian vault and Neo4j graph.

Contract:
  - Every documented node (Project, Course) has a corresponding note.
  - The note carries engrama_id in its YAML frontmatter.
  - engrama_id is the canonical identity link — survives renames and moves.
  - Deleted notes → node archived (status: "archived"), never hard-deleted.
  - New/modified notes → entities extracted → graph updated.

Usage:
    sync = ObsidianSync(engine, adapter)
    sync.full_scan()          # on startup: reconcile entire vault
    sync.sync_note(path)      # on single note change
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .adapter import ObsidianAdapter
from .parser import NoteParser, ParsedNote

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine


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

        Returns a summary dict with created/updated/skipped counts.
        """
        notes = self.adapter.list_notes(recursive=True)
        created = updated = skipped = 0

        for note_meta in notes:
            result = self.sync_note(note_meta["path"])
            if result == "created":
                created += 1
            elif result == "updated":
                updated += 1
            else:
                skipped += 1

        return {"created": created, "updated": updated, "skipped": skipped}

    def sync_note(self, path: str) -> str:
        """Sync a single note to the graph.

        Returns: "created" | "updated" | "skipped"
        """
        note_data = self.adapter.read_note(path)
        if not note_data["success"]:
            return "skipped"

        parsed = self.parser.parse(
            path=path,
            content=note_data["content"],
            frontmatter=note_data["frontmatter"],
        )
        if parsed is None:
            return "skipped"

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
        return "created" if result.get("created") else "updated"

    def archive_missing(self) -> int:
        """Archive graph nodes whose Obsidian notes no longer exist.

        Returns the number of nodes archived.
        """
        # Query nodes that have obsidian_path set
        records = self.engine.run(
            "MATCH (n) WHERE n.obsidian_path IS NOT NULL "
            "RETURN labels(n)[0] AS label, n.name AS name, n.obsidian_path AS path",
            {},
        )
        archived = 0
        for record in records:
            note = self.adapter.read_note(record["path"])
            if not note["success"]:
                # Note deleted — archive the node
                self.engine.run(
                    "MATCH (n {name: $name}) WHERE $label IN labels(n) "
                    "SET n.status = 'archived', n.updated_at = datetime()",
                    {"name": record["name"], "label": record["label"]},
                )
                archived += 1
        return archived
