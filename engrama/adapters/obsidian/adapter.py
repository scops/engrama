"""
engrama/adapters/obsidian/adapter.py

Thin client that wraps Obsidian vault file operations.
Implements direct file I/O to interact with Obsidian vaults so Engrama
maintains bidirectional sync between vault notes and the Neo4j graph.

Environment variables:
    VAULT_PATH   Absolute path to the Obsidian vault root.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

try:
    import yaml

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


class ObsidianAdapter:
    """Programmatic wrapper around Obsidian vault file operations."""

    def __init__(self, vault_path: str | Path | None = None) -> None:
        self.vault_path = Path(
            vault_path or os.environ.get("VAULT_PATH", Path.home() / "Documents/vault")
        ).resolve()
        if not self.vault_path.exists():
            raise FileNotFoundError(f"Vault not found: {self.vault_path}")

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def read_note(self, path: str) -> dict[str, Any]:
        """Read a note and return content + parsed frontmatter."""
        target = self._resolve(path)
        if not target.exists():
            return {"success": False, "error": f"Note not found: {path}"}
        content = target.read_text(encoding="utf-8")
        return {
            "success": True,
            "path": path,
            "content": content,
            "frontmatter": self._parse_frontmatter(content),
        }

    def list_notes(self, folder: str = "", recursive: bool = False) -> list[dict]:
        """List .md files in a vault folder."""
        base = self._resolve(folder) if folder else self.vault_path
        pattern = "**/*.md" if recursive else "*.md"
        return [
            {"path": str(f.relative_to(self.vault_path)), "name": f.stem}
            for f in sorted(base.glob(pattern))
        ]

    def search_notes(self, query: str, folder: str = "") -> list[dict]:
        """Full-text search. Returns path + excerpt per matching note."""
        base = self._resolve(folder) if folder else self.vault_path
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        results = []
        for f in sorted(base.glob("**/*.md")):
            content = f.read_text(encoding="utf-8", errors="ignore")
            matches = list(pattern.finditer(content))
            if matches:
                start = max(0, matches[0].start() - 60)
                end = min(len(content), matches[0].end() + 60)
                excerpt = "..." + content[start:end].replace("\n", " ").strip() + "..."
                results.append(
                    {
                        "path": str(f.relative_to(self.vault_path)),
                        "name": f.stem,
                        "matches": len(matches),
                        "excerpt": excerpt,
                    }
                )
        return results

    # ------------------------------------------------------------------
    # engrama_id contract
    # ------------------------------------------------------------------

    def get_engrama_id(self, path: str) -> str | None:
        """Return the engrama_id from a note's frontmatter, or None."""
        note = self.read_note(path)
        if not note["success"]:
            return None
        return note["frontmatter"].get("engrama_id")

    def inject_engrama_id(self, path: str, engrama_id: str) -> bool:
        """Inject or update engrama_id in the note's YAML frontmatter.

        Returns True if the note was modified, False if already up to date.
        This is part of Engrama's bidirectional sync with Obsidian vaults.
        """
        target = self._resolve(path)
        if not target.exists():
            return False

        content = target.read_text(encoding="utf-8")
        fm = self._parse_frontmatter(content)

        if fm.get("engrama_id") == engrama_id:
            return False  # already correct

        if content.startswith("---"):
            end_idx = content.index("---", 3)
            fm_body = content[3:end_idx]
            if "engrama_id:" in fm_body:
                fm_body = re.sub(
                    r"engrama_id:.*\n",
                    f"engrama_id: {engrama_id}\n",
                    fm_body,
                )
            else:
                fm_body = fm_body.rstrip("\n") + f"\nengrama_id: {engrama_id}\n"
            new_content = "---" + fm_body + "---" + content[end_idx + 3 :]
        else:
            new_content = f"---\nengrama_id: {engrama_id}\n---\n\n" + content

        target.write_text(new_content, encoding="utf-8")
        return True

    # ------------------------------------------------------------------
    # Relations in frontmatter (DDR-002: bidirectional sync)
    # ------------------------------------------------------------------

    def add_relation(
        self,
        path: str,
        rel_type: str,
        target_name: str,
    ) -> bool:
        """Add a relation to a note's YAML frontmatter.

        Appends *target_name* to the ``relations.<rel_type>`` array.
        Creates the ``relations`` map if it doesn't exist yet.
        Idempotent — returns False if the relation already exists.

        Returns True if the frontmatter was modified.
        """
        target = self._resolve(path)
        if not target.exists():
            return False

        content = target.read_text(encoding="utf-8")
        fm = self._parse_frontmatter(content)

        relations: dict = fm.get("relations", {})
        if not isinstance(relations, dict):
            relations = {}

        existing = relations.get(rel_type, [])
        if isinstance(existing, str):
            existing = [existing]
        if target_name in existing:
            return False  # already present

        existing.append(target_name)
        relations[rel_type] = existing

        return self._write_frontmatter_field(path, content, "relations", relations)

    def remove_relation(
        self,
        path: str,
        rel_type: str,
        target_name: str,
    ) -> bool:
        """Remove a relation from a note's YAML frontmatter.

        Returns True if the frontmatter was modified, False if the
        relation was not found.
        """
        target = self._resolve(path)
        if not target.exists():
            return False

        content = target.read_text(encoding="utf-8")
        fm = self._parse_frontmatter(content)

        relations: dict = fm.get("relations", {})
        if not isinstance(relations, dict):
            return False

        existing = relations.get(rel_type, [])
        if isinstance(existing, str):
            existing = [existing]
        if target_name not in existing:
            return False

        existing.remove(target_name)
        if not existing:
            del relations[rel_type]
        else:
            relations[rel_type] = existing

        if not relations:
            return self._remove_frontmatter_field(path, content, "relations")
        return self._write_frontmatter_field(path, content, "relations", relations)

    def set_relations(
        self,
        path: str,
        relations: dict[str, list[str]],
    ) -> bool:
        """Replace the entire ``relations`` map in a note's frontmatter.

        Used during graph→vault sync to write a complete snapshot.
        Returns True if the frontmatter was modified.
        """
        target = self._resolve(path)
        if not target.exists():
            return False

        content = target.read_text(encoding="utf-8")

        if not relations:
            return self._remove_frontmatter_field(path, content, "relations")
        return self._write_frontmatter_field(path, content, "relations", relations)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _write_frontmatter_field(self, path: str, content: str, key: str, value: Any) -> bool:
        """Write or update a single field in a note's YAML frontmatter.

        Requires PyYAML for complex values (dicts, nested lists).
        Returns True if the file was modified.
        """
        target = self._resolve(path)

        if not _HAS_YAML:
            raise RuntimeError("PyYAML is required for writing complex frontmatter values")

        fm = self._parse_frontmatter(content)
        fm[key] = value

        # Rebuild frontmatter YAML
        fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)

        if content.startswith("---"):
            end_idx = content.index("---", 3)
            body = content[end_idx + 3 :]
            # body already starts with \n from the original "---\n"
        else:
            body = "\n\n" + content

        new_content = "---\n" + fm_yaml + "---" + body
        target.write_text(new_content, encoding="utf-8")
        return True

    def _remove_frontmatter_field(self, path: str, content: str, key: str) -> bool:
        """Remove a field from a note's YAML frontmatter.

        Returns True if the file was modified, False if the field was not found.
        """
        target = self._resolve(path)
        fm = self._parse_frontmatter(content)
        if key not in fm:
            return False

        del fm[key]

        if not _HAS_YAML:
            raise RuntimeError("PyYAML is required for writing complex frontmatter values")

        fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)

        if content.startswith("---"):
            end_idx = content.index("---", 3)
            body = content[end_idx + 3 :]
        else:
            body = "\n\n" + content

        new_content = "---\n" + fm_yaml + "---" + body
        target.write_text(new_content, encoding="utf-8")
        return True

    def _resolve(self, relative_path: str) -> Path:
        target = (self.vault_path / relative_path).resolve()
        if not str(target).startswith(str(self.vault_path)):
            raise ValueError(f"Path traversal blocked: {relative_path!r}")
        return target

    def _parse_frontmatter(self, content: str) -> dict[str, Any]:
        """Parse YAML frontmatter into a dict. Returns {} if none found."""
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return {}
        if _HAS_YAML:
            try:
                return yaml.safe_load(match.group(1)) or {}
            except Exception:
                pass
        # Minimal fallback: parse key: value lines
        result: dict[str, Any] = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                result[k.strip()] = v.strip()
        return result
