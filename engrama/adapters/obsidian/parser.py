"""
engrama/adapters/obsidian/parser.py

Extracts Engrama entities from Obsidian note content.

Strategy:
  1. Frontmatter ``engrama_label`` wins — explicit label.
  2. Vault folder structure infers the label (10-projects → Project, etc.).
  3. The note title (H1 or filename stem) becomes the node name.
  4. Wiki-links ([[Target]]) are collected for relation creation by the sync.
  5. Tags are stored as-is on the node.

All notes in the vault are candidates for parsing.  Notes that cannot be
classified are skipped by the sync tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ParsedNote:
    """Result of parsing a single Obsidian note."""

    path: str
    label: str  # Engrama node label
    name: str  # node name / title
    properties: dict[str, Any] = field(default_factory=dict)
    engrama_id: str | None = None
    tags: list[str] = field(default_factory=list)
    wiki_links: list[str] = field(default_factory=list)
    relations: dict[str, list[str]] = field(default_factory=dict)
    raw_content: str = ""


# Frontmatter fields that map directly to node properties per label.
_FIELD_MAP: dict[str, dict[str, str]] = {
    "Project": {
        "status": "status",
        "repo": "repo",
        "stack": "stack",
    },
    "Course": {
        "cohort": "cohort",
        "date": "date",
        "level": "level",
        "client": "client",
    },
}

# Regex: [[link]] or [[link|alias]] — captures the target (before the pipe).
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:[|#][^\]]*?)?\]\]")


class NoteParser:
    """Parses an Obsidian note into a ParsedNote ready for engine ingestion."""

    def parse(self, path: str, content: str, frontmatter: dict[str, Any]) -> ParsedNote | None:
        """Return a ParsedNote or None if the note cannot be classified.

        Args:
            path:        Vault-relative path, e.g. '10-projects/engrama/INDEX.md'
            content:     Full note content including frontmatter.
            frontmatter: Pre-parsed frontmatter dict from ObsidianAdapter.
        """
        label = self._infer_label(path, frontmatter)
        if not label:
            return None

        name = (
            frontmatter.get("title")
            or frontmatter.get("name")
            or self._extract_h1(content)
            or Path(path).stem
        )

        tags_raw = frontmatter.get("tags", [])
        if isinstance(tags_raw, str):
            tags = [t.strip() for t in tags_raw.strip("[]").split(",") if t.strip()]
        else:
            tags = list(tags_raw)

        props: dict[str, Any] = {"name": name}
        for fm_key, node_key in _FIELD_MAP.get(label, {}).items():
            if fm_key in frontmatter:
                props[node_key] = frontmatter[fm_key]

        # Include description extracted from first non-frontmatter paragraph
        description = self._extract_description(content)
        if description:
            props["description"] = description

        # Extract wiki-links for relation creation
        wiki_links = self._extract_wiki_links(content)

        # Extract relations from frontmatter (DDR-002: bidirectional sync)
        relations = self._extract_relations(frontmatter)

        return ParsedNote(
            path=path,
            label=label,
            name=name,
            properties=props,
            engrama_id=frontmatter.get("engrama_id"),
            tags=tags,
            wiki_links=wiki_links,
            relations=relations,
            raw_content=content,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_label(path: str, frontmatter: dict[str, Any]) -> str | None:
        """Infer Engrama label from explicit frontmatter or vault path."""
        # Explicit label wins
        if "engrama_label" in frontmatter:
            return frontmatter["engrama_label"]

        # Infer from vault path prefix
        path_lower = path.lower()
        if path_lower.startswith("10-projects"):
            return "Project"
        if path_lower.startswith("50-cursos"):
            return "Course"
        if path_lower.startswith("20-areas"):
            return "Concept"

        return None

    @staticmethod
    def _extract_h1(content: str) -> str | None:
        """Return the first H1 heading text from Markdown content."""
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else None

    @staticmethod
    def _extract_description(content: str) -> str | None:
        """Return the first blockquote or paragraph after the frontmatter."""
        # Strip frontmatter
        body = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL).strip()
        # Skip H1
        body = re.sub(r"^#.+\n", "", body).strip()
        # Blockquote (> text)
        bq = re.match(r"^>\s*(.+)", body)
        if bq:
            return bq.group(1).strip()
        # First non-empty paragraph (up to 200 chars)
        para = re.match(r"^([^\n#>].+)", body)
        if para:
            return para.group(1).strip()[:200]
        return None

    @staticmethod
    def _extract_relations(frontmatter: dict[str, Any]) -> dict[str, list[str]]:
        """Extract relations map from frontmatter.

        Expected format::

            relations:
              INSTANCE_OF: [concept-a]
              USES: [Python, Neo4j]

        Returns a dict mapping relationship type → list of target node names.
        Normalises scalar values to single-element lists.
        """
        raw = frontmatter.get("relations")
        if not raw or not isinstance(raw, dict):
            return {}
        result: dict[str, list[str]] = {}
        for rel_type, targets in raw.items():
            rel_type = str(rel_type).upper()
            if isinstance(targets, str):
                targets = [targets]
            elif not isinstance(targets, list):
                continue
            cleaned = [str(t).strip() for t in targets if t]
            if cleaned:
                result[rel_type] = cleaned
        return result

    @staticmethod
    def _extract_wiki_links(content: str) -> list[str]:
        """Extract all wiki-link targets from note content.

        Handles [[Target]], [[Target|alias]], and [[Target#section]].
        Returns deduplicated list preserving first-seen order.
        """
        seen: set[str] = set()
        result: list[str] = []
        for match in _WIKILINK_RE.finditer(content):
            target = match.group(1).strip()
            if target and target not in seen:
                seen.add(target)
                result.append(target)
        return result
