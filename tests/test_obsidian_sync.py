"""
tests/test_obsidian_sync.py

Integration tests for the Obsidian adapter and sync engine.
Requires VAULT_PATH env var pointing to a real or test vault.
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
