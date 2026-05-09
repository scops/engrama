"""Tests for the SQLite graph store backend.

Runs without any external service — exercises an in-memory SQLite
database so CI doesn't need Neo4j, Docker, or filesystem state.
"""

from __future__ import annotations

import pytest

from engrama.backends.sqlite import SqliteGraphStore


@pytest.fixture()
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "test.db")
    yield s
    s.close()


# ----------------------------------------------------------------------
# Lifecycle / health
# ----------------------------------------------------------------------


def test_health_check_reports_sqlite(store):
    h = store.health_check()
    assert h["ok"] is True
    assert h["backend"] == "sqlite"
    assert h["node_count"] == 0
    assert h["sqlite_version"]


def test_init_schema_is_idempotent(store):
    # Connection ctor already initialised the schema; this should be safe.
    store.init_schema()
    store.init_schema()
    assert store.health_check()["ok"]


# ----------------------------------------------------------------------
# Node operations
# ----------------------------------------------------------------------


def test_merge_node_creates(store):
    result = store.merge_node(
        "Project", "name", "test-proj",
        {"status": "active", "description": "demo"},
    )
    assert len(result) == 1
    n = result[0]["n"]
    assert n["_labels"] == ["Project"]
    assert n["name"] == "test-proj"
    assert n["status"] == "active"
    assert n["created_at"] == n["updated_at"]
    assert n["confidence"] == 1.0  # default
    assert n["valid_from"]


def test_merge_node_updates_preserves_created_at(store):
    a = store.merge_node("Project", "name", "p1", {"status": "active"})[0]["n"]
    b = store.merge_node("Project", "name", "p1", {"status": "paused"})[0]["n"]
    assert a["_id"] == b["_id"]
    assert b["created_at"] == a["created_at"]
    assert b["updated_at"] >= a["updated_at"]
    assert b["status"] == "paused"


def test_merge_node_merges_props_does_not_drop(store):
    store.merge_node("Project", "name", "p1", {"status": "active", "stack": ["python"]})
    n = store.merge_node("Project", "name", "p1", {"description": "added"})[0]["n"]
    assert n["status"] == "active"          # preserved
    assert n["stack"] == ["python"]         # preserved
    assert n["description"] == "added"      # added


def test_get_node(store):
    store.merge_node("Concept", "name", "graphs", {"domain": "cs"})
    n = store.get_node("Concept", "name", "graphs")
    assert n["domain"] == "cs"
    assert n["name"] == "graphs"
    assert n["created_at"]
    assert store.get_node("Concept", "name", "missing") is None


def test_delete_node_soft(store):
    store.merge_node("Project", "name", "p1", {})
    assert store.delete_node("Project", "name", "p1", soft=True) is True
    n = store.get_node("Project", "name", "p1")
    assert n["status"] == "archived"
    assert n["archived_at"]


def test_delete_node_hard(store):
    store.merge_node("Project", "name", "p1", {})
    assert store.delete_node("Project", "name", "p1", soft=False) is True
    assert store.get_node("Project", "name", "p1") is None


def test_archive_node_by_name(store):
    store.merge_node("Project", "name", "to-forget", {"status": "active"})
    out = store.archive_node_by_name("Project", "to-forget")
    assert out["archived"] is True
    assert out["node"]["name"] == "to-forget"
    assert out["node"]["archived_at"]
    assert store.archive_node_by_name("Project", "missing")["archived"] is False


def test_list_existing_nodes(store):
    store.merge_node("Project", "name", "alpha", {})
    store.merge_node("Concept", "name", "beta", {})
    out = store.list_existing_nodes()
    assert {"label": "Project", "name": "alpha"} in out
    assert {"label": "Concept", "name": "beta"} in out


# ----------------------------------------------------------------------
# Relationship operations
# ----------------------------------------------------------------------


def test_merge_relation_creates(store):
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Technology", "name", "python", {})
    out = store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "python")
    assert out and out[0]["rel_type"] == "USES"


def test_merge_relation_idempotent(store):
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Technology", "name", "python", {})
    store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "python")
    store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "python")
    # Only one neighbour should appear.
    rows = store.get_neighbours("Project", "name", "p", hops=1)
    assert len(rows) == 1


def test_merge_relation_silent_when_endpoint_missing(store):
    store.merge_node("Project", "name", "p", {})
    out = store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "missing")
    assert out == []


# ----------------------------------------------------------------------
# Traversal
# ----------------------------------------------------------------------


def test_get_neighbours_one_hop(store):
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Technology", "name", "python", {})
    store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "python")
    rows = store.get_neighbours("Project", "name", "p", hops=1)
    assert len(rows) == 1
    row = rows[0]
    assert row["start"]["_labels"] == ["Project"]
    assert row["neighbour"]["_labels"] == ["Technology"]
    assert row["neighbour"]["name"] == "python"
    assert row["rel"][0]["_type"] == "USES"


def test_get_neighbours_two_hops_reaches_further(store):
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Technology", "name", "python", {})
    store.merge_node("Concept", "name", "graphs", {})
    store.merge_relation("Project",   "name", "p",      "USES",     "Technology", "name", "python")
    store.merge_relation("Technology", "name", "python", "APPLIES", "Concept",    "name", "graphs")
    one_hop = store.get_neighbours("Project", "name", "p", hops=1)
    two_hop = store.get_neighbours("Project", "name", "p", hops=2)
    one_hop_names = {r["neighbour"]["name"] for r in one_hop}
    two_hop_names = {r["neighbour"]["name"] for r in two_hop}
    assert one_hop_names == {"python"}
    assert two_hop_names == {"python", "graphs"}


def test_get_neighbours_traverses_undirected(store):
    """Edges walked in both directions, mirroring Neo4j ``-[r*1..N]-``."""
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Person",  "name", "alice", {})
    # Edge points alice -> p; querying from p must still find alice.
    store.merge_relation("Person", "name", "alice", "BELONGS_TO", "Project", "name", "p")
    rows = store.get_neighbours("Project", "name", "p", hops=1)
    assert {r["neighbour"]["name"] for r in rows} == {"alice"}


def test_get_node_with_neighbours(store):
    store.merge_node("Project", "name", "p", {"description": "the proj"})
    store.merge_node("Technology", "name", "python", {})
    store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "python")
    out = store.get_node_with_neighbours("Project", "name", "p", hops=1)
    assert out["node"]["name"] == "p"
    assert out["node"]["description"] == "the proj"
    assert out["neighbours"][0]["name"] == "python"
    assert out["neighbours"][0]["via"] == ["USES"]


def test_get_node_with_neighbours_returns_none_for_missing(store):
    assert store.get_node_with_neighbours("Project", "name", "ghost") is None


# ----------------------------------------------------------------------
# Lookup helpers
# ----------------------------------------------------------------------


def test_lookup_node_label_finds_by_name_or_title(store):
    store.merge_node("Project",  "name",  "alpha", {})
    store.merge_node("Decision", "title", "go-rest", {})
    assert store.lookup_node_label("alpha") == "Project"
    assert store.lookup_node_label("go-rest") == "Decision"
    assert store.lookup_node_label("missing") is None


def test_lookup_node_label_is_case_insensitive(store):
    store.merge_node("Project", "name", "MixedCase", {})
    assert store.lookup_node_label("mixedcase") == "Project"


def test_count_labels_excludes_insights(store):
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Project", "name", "q", {})
    store.merge_node("Insight", "title", "i1", {})
    counts = store.count_labels()
    assert counts.get("Project") == 2
    assert "Insight" not in counts


# ----------------------------------------------------------------------
# Fulltext
# ----------------------------------------------------------------------


def test_fulltext_search_matches_description(store):
    store.merge_node("Project", "name", "alpha",
                     {"description": "graph database memory engine"})
    out = store.fulltext_search("memory")
    assert any(r["name"] == "alpha" for r in out)


def test_fulltext_search_returns_summary_or_description(store):
    store.merge_node("Project", "name", "alpha",
                     {"description": "the description", "summary": "the summary"})
    store.merge_node("Project", "name", "beta",
                     {"description": "only a description here"})
    out = store.fulltext_search("description")
    by_name = {r["name"]: r for r in out}
    assert by_name["alpha"]["summary"] == "the summary"
    assert by_name["beta"]["summary"] == "only a description here"


def test_fulltext_search_empty_query_returns_empty(store):
    store.merge_node("Project", "name", "p", {"description": "x"})
    assert store.fulltext_search("") == []
    assert store.fulltext_search("   ") == []


def test_fulltext_search_invalid_syntax_returns_empty(store):
    """A malformed FTS5 query must not raise — caller-friendly degradation."""
    store.merge_node("Project", "name", "p", {"description": "x"})
    assert store.fulltext_search('"unbalanced') == []


def test_fulltext_indexes_tags_as_text(store):
    store.merge_node("Project", "name", "alpha", {"tags": ["security", "graphdb"]})
    out = store.fulltext_search("security")
    assert any(r["name"] == "alpha" for r in out)


# ----------------------------------------------------------------------
# Cypher escape hatch
# ----------------------------------------------------------------------


def test_run_cypher_is_not_supported(store):
    with pytest.raises(NotImplementedError):
        store.run_cypher("MATCH (n) RETURN n")
