"""
tests/test_obsidian_sync.py

Integration tests for the Obsidian adapter, parser, and DDR-002 bidirectional
sync (relations in frontmatter).
"""

import os
import tempfile
import textwrap
from pathlib import Path

import pytest

from engrama.adapters.obsidian.adapter import ObsidianAdapter
from engrama.adapters.obsidian.parser import NoteParser


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """Create a minimal temporary vault for testing."""
    (tmp_path / "10-projects").mkdir()
    (tmp_path / "50-cursos").mkdir()

    # Project note with frontmatter
    (tmp_path / "10-projects" / "engrama.md").write_text(
        textwrap.dedent("""\
            ---
            date: 2026-04-11
            tags: [engrama, proyecto]
            status: active
            repo: github.com/scops/engrama
            ---
            # Engrama

            > Graph-based long-term memory framework for AI agents.

            More content here.
        """),
        encoding="utf-8",
    )

    # Course note
    (tmp_path / "50-cursos" / "mcp-mar-26.md").write_text(
        textwrap.dedent("""\
            ---
            date: 2026-03-10
            tags: [mcp, curso]
            cohort: mcp-mar-26
            level: advanced
            ---
            # MCP March 2026

            Advanced MCP course covering FastMCP and Neo4j integration.
        """),
        encoding="utf-8",
    )

    # Note that should be skipped (not a documented label)
    (tmp_path / "00-inbox").mkdir()
    (tmp_path / "00-inbox" / "random-idea.md").write_text(
        "# Random idea\nJust a draft.", encoding="utf-8"
    )

    # DDR-002: Project note with relations in frontmatter
    (tmp_path / "10-projects" / "eoelite.md").write_text(
        textwrap.dedent("""\
            ---
            date: 2026-04-12
            tags: [eoelite, proyecto]
            status: active
            engrama_id: test-uuid-eoelite
            relations:
              USES: [Python, Neo4j]
              IN_DOMAIN: [web-development]
              BELONGS_TO: [EOElite]
            ---
            # EOElite

            > Online training platform for ethical hacking.

            Built with Python and Neo4j.
        """),
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture
def adapter(tmp_vault: Path) -> ObsidianAdapter:
    return ObsidianAdapter(vault_path=tmp_vault)


# ── Adapter tests ─────────────────────────────────────────────────────────────

def test_read_note_success(adapter: ObsidianAdapter):
    result = adapter.read_note("10-projects/engrama.md")
    assert result["success"] is True
    assert "Engrama" in result["content"]
    assert result["frontmatter"]["status"] == "active"


def test_read_note_missing(adapter: ObsidianAdapter):
    result = adapter.read_note("10-projects/nonexistent.md")
    assert result["success"] is False


def test_list_notes_recursive(adapter: ObsidianAdapter):
    notes = adapter.list_notes(recursive=True)
    paths = [n["path"] for n in notes]
    assert any("engrama.md" in p for p in paths)
    assert any("mcp-mar-26.md" in p for p in paths)


def test_search_notes(adapter: ObsidianAdapter):
    results = adapter.search_notes("FastMCP")
    assert len(results) == 1
    assert "mcp-mar-26" in results[0]["path"]
    assert "FastMCP" in results[0]["excerpt"]


def test_inject_engrama_id_new(adapter: ObsidianAdapter, tmp_vault: Path):
    path = "10-projects/engrama.md"
    modified = adapter.inject_engrama_id(path, "test-uuid-1234")
    assert modified is True
    assert adapter.get_engrama_id(path) == "test-uuid-1234"


def test_inject_engrama_id_idempotent(adapter: ObsidianAdapter):
    path = "10-projects/engrama.md"
    adapter.inject_engrama_id(path, "uuid-abc")
    modified_again = adapter.inject_engrama_id(path, "uuid-abc")
    assert modified_again is False  # no change needed


def test_inject_engrama_id_update(adapter: ObsidianAdapter):
    path = "10-projects/engrama.md"
    adapter.inject_engrama_id(path, "old-uuid")
    modified = adapter.inject_engrama_id(path, "new-uuid")
    assert modified is True
    assert adapter.get_engrama_id(path) == "new-uuid"


# ── Parser tests ──────────────────────────────────────────────────────────────

def test_parse_project_note(adapter: ObsidianAdapter):
    parser = NoteParser()
    note = adapter.read_note("10-projects/engrama.md")
    parsed = parser.parse(
        path="10-projects/engrama.md",
        content=note["content"],
        frontmatter=note["frontmatter"],
    )
    assert parsed is not None
    assert parsed.label == "Project"
    assert parsed.name == "Engrama"
    assert parsed.properties["status"] == "active"
    assert parsed.properties["repo"] == "github.com/scops/engrama"
    assert "Graph-based" in parsed.properties.get("description", "")


def test_parse_course_note(adapter: ObsidianAdapter):
    parser = NoteParser()
    note = adapter.read_note("50-cursos/mcp-mar-26.md")
    parsed = parser.parse(
        path="50-cursos/mcp-mar-26.md",
        content=note["content"],
        frontmatter=note["frontmatter"],
    )
    assert parsed is not None
    assert parsed.label == "Course"
    assert parsed.properties["cohort"] == "mcp-mar-26"
    assert parsed.properties["level"] == "advanced"


def test_parse_inbox_note_skipped(adapter: ObsidianAdapter):
    parser = NoteParser()
    note = adapter.read_note("00-inbox/random-idea.md")
    parsed = parser.parse(
        path="00-inbox/random-idea.md",
        content=note["content"],
        frontmatter=note["frontmatter"],
    )
    assert parsed is None  # not a documentable label


# ── DDR-002: Relations in frontmatter ─────────────────────────────────────────

class TestParserRelations:
    """Parser should extract relations from frontmatter (DDR-002)."""

    def test_parse_note_with_relations(self, adapter: ObsidianAdapter):
        parser = NoteParser()
        note = adapter.read_note("10-projects/eoelite.md")
        parsed = parser.parse(
            path="10-projects/eoelite.md",
            content=note["content"],
            frontmatter=note["frontmatter"],
        )
        assert parsed is not None
        assert parsed.relations == {
            "USES": ["Python", "Neo4j"],
            "IN_DOMAIN": ["web-development"],
            "BELONGS_TO": ["EOElite"],
        }

    def test_parse_note_without_relations(self, adapter: ObsidianAdapter):
        parser = NoteParser()
        note = adapter.read_note("10-projects/engrama.md")
        parsed = parser.parse(
            path="10-projects/engrama.md",
            content=note["content"],
            frontmatter=note["frontmatter"],
        )
        assert parsed is not None
        assert parsed.relations == {}

    def test_parse_relations_scalar_to_list(self, tmp_path: Path):
        """A scalar value in relations should be normalised to a list."""
        (tmp_path / "10-projects").mkdir(exist_ok=True)
        (tmp_path / "10-projects" / "scalar-rel.md").write_text(
            textwrap.dedent("""\
                ---
                engrama_label: Project
                name: ScalarTest
                relations:
                  IN_DOMAIN: cybersecurity
                ---
                # Scalar Test
            """),
            encoding="utf-8",
        )
        adapter = ObsidianAdapter(vault_path=tmp_path)
        parser = NoteParser()
        note = adapter.read_note("10-projects/scalar-rel.md")
        parsed = parser.parse(
            path="10-projects/scalar-rel.md",
            content=note["content"],
            frontmatter=note["frontmatter"],
        )
        assert parsed is not None
        assert parsed.relations == {"IN_DOMAIN": ["cybersecurity"]}


class TestAdapterRelations:
    """Adapter should read/write relations in frontmatter (DDR-002)."""

    def test_add_relation_new(self, adapter: ObsidianAdapter):
        path = "10-projects/engrama.md"
        modified = adapter.add_relation(path, "USES", "Python")
        assert modified is True

        # Verify it was written
        note = adapter.read_note(path)
        assert note["frontmatter"]["relations"]["USES"] == ["Python"]

    def test_add_relation_idempotent(self, adapter: ObsidianAdapter):
        path = "10-projects/eoelite.md"
        # "Python" already in USES
        modified = adapter.add_relation(path, "USES", "Python")
        assert modified is False

    def test_add_relation_appends(self, adapter: ObsidianAdapter):
        path = "10-projects/eoelite.md"
        modified = adapter.add_relation(path, "USES", "FastMCP")
        assert modified is True

        note = adapter.read_note(path)
        uses = note["frontmatter"]["relations"]["USES"]
        assert "Python" in uses
        assert "Neo4j" in uses
        assert "FastMCP" in uses

    def test_add_relation_new_type(self, adapter: ObsidianAdapter):
        path = "10-projects/engrama.md"
        adapter.add_relation(path, "COMPOSED_OF", "Neo4j")
        adapter.add_relation(path, "IN_DOMAIN", "ai")

        note = adapter.read_note(path)
        rels = note["frontmatter"]["relations"]
        assert rels["COMPOSED_OF"] == ["Neo4j"]
        assert rels["IN_DOMAIN"] == ["ai"]

    def test_remove_relation(self, adapter: ObsidianAdapter):
        path = "10-projects/eoelite.md"
        modified = adapter.remove_relation(path, "USES", "Neo4j")
        assert modified is True

        note = adapter.read_note(path)
        assert "Neo4j" not in note["frontmatter"]["relations"]["USES"]
        assert "Python" in note["frontmatter"]["relations"]["USES"]

    def test_remove_last_relation_cleans_type(self, adapter: ObsidianAdapter):
        path = "10-projects/eoelite.md"
        adapter.remove_relation(path, "IN_DOMAIN", "web-development")

        note = adapter.read_note(path)
        assert "IN_DOMAIN" not in note["frontmatter"]["relations"]

    def test_remove_nonexistent_relation(self, adapter: ObsidianAdapter):
        path = "10-projects/eoelite.md"
        modified = adapter.remove_relation(path, "USES", "Rust")
        assert modified is False

    def test_set_relations_full_replace(self, adapter: ObsidianAdapter):
        path = "10-projects/engrama.md"
        new_rels = {
            "USES": ["Python", "Neo4j", "FastMCP"],
            "IN_DOMAIN": ["ai", "cybersecurity"],
        }
        modified = adapter.set_relations(path, new_rels)
        assert modified is True

        note = adapter.read_note(path)
        rels = note["frontmatter"]["relations"]
        assert rels == new_rels

    def test_set_relations_empty_removes(self, adapter: ObsidianAdapter):
        path = "10-projects/eoelite.md"
        modified = adapter.set_relations(path, {})
        assert modified is True

        note = adapter.read_note(path)
        assert "relations" not in note["frontmatter"]

    def test_add_relation_preserves_content(self, adapter: ObsidianAdapter):
        """Adding a relation should not corrupt the note body."""
        path = "10-projects/engrama.md"
        original = adapter.read_note(path)
        adapter.add_relation(path, "USES", "Python")
        updated = adapter.read_note(path)

        # Body content should still be there
        assert "Graph-based long-term memory framework" in updated["content"]
        assert "More content here." in updated["content"]

    def test_add_relation_preserves_other_frontmatter(self, adapter: ObsidianAdapter):
        """Adding a relation should not lose existing frontmatter fields."""
        path = "10-projects/engrama.md"
        adapter.add_relation(path, "USES", "Python")
        note = adapter.read_note(path)
        assert note["frontmatter"]["status"] == "active"
        assert note["frontmatter"]["repo"] == "github.com/scops/engrama"
