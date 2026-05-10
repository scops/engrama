"""Tests for the SQLite vector store (sqlite-vec)."""

from __future__ import annotations

import pytest

from engrama.backends.sqlite import SqliteGraphStore, SqliteVecStore


@pytest.fixture()
def store_pair(tmp_path):
    """Graph + vector store sharing the same SQLite connection."""
    graph = SqliteGraphStore(tmp_path / "vec.db")
    vec = SqliteVecStore(graph._conn, dimensions=4)
    vec.ensure_index()
    yield graph, vec
    graph.close()


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


def test_dimensions_attribute(store_pair):
    _, vec = store_pair
    assert vec.dimensions == 4


def test_zero_dimensions_makes_ops_no_op(tmp_path):
    graph = SqliteGraphStore(tmp_path / "z.db")
    vec = SqliteVecStore(graph._conn, dimensions=0)
    assert vec.store_vectors([("1", [0.0] * 4)]) == 0
    assert vec.search_vectors([0.0] * 4) == []
    assert vec.count() == 0
    graph.close()


def test_ensure_index_is_idempotent(store_pair):
    _, vec = store_pair
    vec.ensure_index()
    vec.ensure_index()
    assert vec.count() == 0


# ----------------------------------------------------------------------
# Round-trip
# ----------------------------------------------------------------------


def test_store_and_search_vectors(store_pair):
    graph, vec = store_pair
    a = graph.merge_node("Project", "name", "alpha", {})[0]["n"]
    b = graph.merge_node("Project", "name", "beta", {})[0]["n"]
    vec.store_vectors(
        [
            (a["_id"], [1.0, 0.0, 0.0, 0.0]),
            (b["_id"], [0.0, 1.0, 0.0, 0.0]),
        ]
    )
    assert vec.count() == 2
    out = vec.search_vectors([1.0, 0.0, 0.0, 0.0], limit=1)
    assert len(out) == 1
    assert out[0]["key"] == "alpha"
    assert out[0]["label"] == "Project"
    assert out[0]["score"] == pytest.approx(1.0)


def test_store_vector_by_key_resolves_node_id(store_pair):
    graph, vec = store_pair
    graph.merge_node("Project", "name", "p", {})
    assert vec.store_vector_by_key("Project", "name", "p", [1, 0, 0, 0]) is True
    assert vec.count() == 1


def test_store_vector_by_key_missing_node_returns_false(store_pair):
    _, vec = store_pair
    assert vec.store_vector_by_key("Project", "name", "ghost", [1, 0, 0, 0]) is False


def test_delete_vectors(store_pair):
    graph, vec = store_pair
    a = graph.merge_node("Project", "name", "a", {})[0]["n"]
    b = graph.merge_node("Project", "name", "b", {})[0]["n"]
    vec.store_vectors([(a["_id"], [1, 0, 0, 0]), (b["_id"], [0, 1, 0, 0])])
    deleted = vec.delete_vectors([a["_id"]])
    assert deleted == 1
    assert vec.count() == 1


def test_search_similar_alias(store_pair):
    graph, vec = store_pair
    a = graph.merge_node("Project", "name", "a", {})[0]["n"]
    vec.store_vectors([(a["_id"], [1, 0, 0, 0])])
    out = vec.search_similar([1, 0, 0, 0], limit=1)
    assert out and out[0]["key"] == "a"


def test_count_embeddings_alias(store_pair):
    _, vec = store_pair
    assert vec.count_embeddings() == 0


def test_search_vectors_orders_by_similarity(store_pair):
    """Closest match comes first."""
    graph, vec = store_pair
    a = graph.merge_node("Project", "name", "match", {})[0]["n"]
    b = graph.merge_node("Project", "name", "far", {})[0]["n"]
    vec.store_vectors(
        [
            (a["_id"], [1.0, 0.0, 0.0, 0.0]),
            (b["_id"], [-1.0, 0.0, 0.0, 0.0]),
        ]
    )
    out = vec.search_vectors([1.0, 0.0, 0.0, 0.0], limit=2)
    assert [r["key"] for r in out] == ["match", "far"]
