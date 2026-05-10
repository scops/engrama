"""
GraphStore contract test suite.

Every backend that claims to implement the ``GraphStore`` protocol
must pass these tests. They define the *behaviour* the engine and
skills depend on; pure ``hasattr``-style checks live in
``test_protocols.py``.

Currently parameterised over: ``sqlite``, ``neo4j`` (skipped if
``NEO4J_PASSWORD`` is unset). Add a new ``request.param`` branch in
``store`` to wire a third backend.

Each Neo4j test uses ``test=true`` props so the conftest cleanup pass
removes them between tests.
"""

from __future__ import annotations

import uuid

import pytest


def _unique(prefix: str = "ct") -> str:
    """Unique value per test invocation so SQLite + Neo4j fixtures
    sharing a process don't collide on key_value.
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(params=["sqlite", "neo4j"])
def store(request, tmp_path):
    if request.param == "sqlite":
        from engrama.backends.sqlite import SqliteGraphStore
        s = SqliteGraphStore(tmp_path / "contract.db")
        yield s
        s.close()
        return

    if request.param == "neo4j":
        import os
        if not os.getenv("NEO4J_PASSWORD"):
            pytest.skip("Neo4j not configured (set NEO4J_PASSWORD to run)")
        # Lazy imports — module-level neo4j import would fail when the
        # neo4j extra is not installed.
        from engrama.backends.neo4j.backend import Neo4jGraphStore
        from engrama.core.client import EngramaClient
        client = EngramaClient()
        s = Neo4jGraphStore(client)
        # Tag every created node with test=true so the conftest cleanup
        # pass removes it. Wraps merge_node transparently.
        original_merge = s.merge_node

        def _tagged_merge(label, key_field, key_value, properties, embedding=None):
            tagged = dict(properties)
            tagged.setdefault("test", True)
            return original_merge(label, key_field, key_value, tagged, embedding=embedding)

        s.merge_node = _tagged_merge  # type: ignore[method-assign]
        yield s
        try:
            s.client.run("MATCH (n) WHERE n.test = true DETACH DELETE n")
        except Exception:
            pass
        client.close()
        return

    raise ValueError(f"unknown backend {request.param!r}")


# ----------------------------------------------------------------------
# Node lifecycle
# ----------------------------------------------------------------------


def test_merge_node_returns_dict_shaped_record(store):
    name = _unique("p")
    out = store.merge_node("Project", "name", name, {"description": "x"})
    assert isinstance(out, list) and len(out) == 1
    n = out[0]["n"]
    assert isinstance(n, dict)
    assert n["name"] == name
    assert n["description"] == "x"
    # Driver-internal types must NOT leak (Phase 1 invariant).
    for v in n.values():
        assert "neo4j" not in type(v).__module__


def test_merge_node_is_idempotent(store):
    name = _unique("p")
    store.merge_node("Project", "name", name, {"status": "active"})
    second = store.merge_node("Project", "name", name, {"status": "paused"})[0]["n"]
    assert second["name"] == name
    assert second["status"] == "paused"


def test_merge_node_merges_props_does_not_drop(store):
    name = _unique("p")
    store.merge_node("Project", "name", name, {"status": "active"})
    after = store.merge_node("Project", "name", name, {"description": "added"})[0]["n"]
    assert after["status"] == "active"
    assert after["description"] == "added"


def test_get_node_returns_props(store):
    name = _unique("p")
    store.merge_node("Project", "name", name, {"description": "demo"})
    n = store.get_node("Project", "name", name)
    assert n is not None
    assert n["description"] == "demo"
    assert store.get_node("Project", "name", _unique("missing")) is None


def test_delete_node_soft_marks_archived(store):
    name = _unique("p")
    store.merge_node("Project", "name", name, {})
    assert store.delete_node("Project", "name", name, soft=True) is True
    n = store.get_node("Project", "name", name)
    assert n is not None
    assert n["status"] == "archived"
    assert n.get("archived_at")


def test_delete_node_hard_removes(store):
    name = _unique("p")
    store.merge_node("Project", "name", name, {})
    assert store.delete_node("Project", "name", name, soft=False) is True
    assert store.get_node("Project", "name", name) is None


# ----------------------------------------------------------------------
# Relationships
# ----------------------------------------------------------------------


def test_merge_relation_idempotent(store):
    a = _unique("a")
    b = _unique("b")
    store.merge_node("Project",    "name", a, {})
    store.merge_node("Technology", "name", b, {})
    store.merge_relation("Project", "name", a, "USES", "Technology", "name", b)
    store.merge_relation("Project", "name", a, "USES", "Technology", "name", b)
    rows = store.get_neighbours("Project", "name", a, hops=1)
    matching = [r for r in rows if r["neighbour"].get("name") == b]
    assert len(matching) == 1


def test_get_neighbours_one_hop(store):
    a = _unique("a")
    b = _unique("b")
    store.merge_node("Project",    "name", a, {})
    store.merge_node("Technology", "name", b, {})
    store.merge_relation("Project", "name", a, "USES", "Technology", "name", b)
    rows = store.get_neighbours("Project", "name", a, hops=1)
    assert any(r["neighbour"].get("name") == b for r in rows)


def test_get_neighbours_two_hops_reaches_further(store):
    a = _unique("a")
    b = _unique("b")
    c = _unique("c")
    store.merge_node("Project",    "name", a, {})
    store.merge_node("Technology", "name", b, {})
    store.merge_node("Concept",    "name", c, {})
    store.merge_relation("Project",    "name", a, "USES",    "Technology", "name", b)
    store.merge_relation("Technology", "name", b, "APPLIES", "Concept",    "name", c)
    one = {
        r["neighbour"].get("name")
        for r in store.get_neighbours("Project", "name", a, hops=1)
    }
    two = {
        r["neighbour"].get("name")
        for r in store.get_neighbours("Project", "name", a, hops=2)
    }
    assert b in one
    assert c not in one
    assert {b, c}.issubset(two)


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------


def test_fulltext_search_matches_description(store):
    name = _unique("ftsdesc")
    needle = f"contractneedle{uuid.uuid4().hex[:6]}"
    store.merge_node("Project", "name", name, {"description": f"a {needle} marker"})
    out = store.fulltext_search(needle)
    assert any(r["name"] == name for r in out)


# ----------------------------------------------------------------------
# Lookup
# ----------------------------------------------------------------------


def test_lookup_node_label_is_case_insensitive(store):
    name = _unique("Mixed")
    store.merge_node("Project", "name", name, {})
    assert store.lookup_node_label(name.lower()) == "Project"
    assert store.lookup_node_label(_unique("missing")) is None


def test_count_labels_excludes_insights(store):
    pname = _unique("p")
    iname = _unique("i")
    store.merge_node("Project", "name",  pname, {})
    store.merge_node("Insight", "title", iname, {"status": "pending"})
    counts = store.count_labels()
    assert counts.get("Project", 0) >= 1
    assert "Insight" not in counts
