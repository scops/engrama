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
from typing import Any

import pytest

from engrama.core.scope import MemoryScope

# Spec 001 fail-closed: writes auto-stamp this test scope; scoped reads
# auto-forward it. The contract under test is "backend behaves the same
# across backends" — keeping scoping at the fixture boundary lets the
# tests stay focused on shape and semantics.
_TEST_SCOPE = MemoryScope(org_id="test-contract", user_id="test-contract")
_SCOPE_PROPS = {"org_id": "test-contract", "user_id": "test-contract"}


def _unique(prefix: str = "ct") -> str:
    """Unique value per test invocation so SQLite + Neo4j fixtures
    sharing a process don't collide on key_value.
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class _ScopedSyncStoreProxy:
    """Auto-stamp scope on ``merge_node``; forward scope to scoped reads."""

    def __init__(self, inner, scope: MemoryScope, *, add_test_flag: bool = False) -> None:
        self._inner = inner
        self._scope = scope
        self._add_test_flag = add_test_flag

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def merge_node(self, label, key_field, key_value, properties, embedding=None):
        properties = {**_SCOPE_PROPS, **properties}
        if self._add_test_flag:
            properties.setdefault("test", True)
        return self._inner.merge_node(label, key_field, key_value, properties, embedding)

    def merge_relation(
        self, from_label, from_key, from_value, rel_type, to_label, to_key, to_value, scope=None
    ):
        return self._inner.merge_relation(
            from_label,
            from_key,
            from_value,
            rel_type,
            to_label,
            to_key,
            to_value,
            scope=scope or self._scope,
        )

    def count_labels(self, scope=None):
        return self._inner.count_labels(scope=scope or self._scope)

    def lookup_node_label(self, name, scope=None):
        return self._inner.lookup_node_label(name, scope=scope or self._scope)

    def fulltext_search(self, query, limit=10, scope=None):
        return self._inner.fulltext_search(query, limit=limit, scope=scope or self._scope)

    def get_neighbours(self, label, key_field, key_value, hops=1, limit=50, scope=None):
        return self._inner.get_neighbours(
            label, key_field, key_value, hops=hops, limit=limit, scope=scope or self._scope
        )

    def get_node_with_neighbours(self, label, key_field, key_value, hops=1, scope=None):
        return self._inner.get_node_with_neighbours(
            label, key_field, key_value, hops=hops, scope=scope or self._scope
        )

    def get_dismissed_insight_titles(self, scope=None):
        return self._inner.get_dismissed_insight_titles(scope=scope or self._scope)

    def get_approved_insight_titles(self, scope=None):
        return self._inner.get_approved_insight_titles(scope=scope or self._scope)

    def get_pending_insights(self, limit=10, scope=None):
        return self._inner.get_pending_insights(limit=limit, scope=scope or self._scope)

    def get_insight_by_title(self, title, scope=None):
        return self._inner.get_insight_by_title(title, scope=scope or self._scope)

    def find_insight_by_source_query(self, source_query, statuses=None, scope=None):
        return self._inner.find_insight_by_source_query(
            source_query, statuses=statuses, scope=scope or self._scope
        )


@pytest.fixture(params=["sqlite", "neo4j"])
def store(request, tmp_path):
    if request.param == "sqlite":
        from engrama.backends.sqlite import SqliteGraphStore

        s = SqliteGraphStore(tmp_path / "contract.db")
        yield _ScopedSyncStoreProxy(s, _TEST_SCOPE)
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
        yield _ScopedSyncStoreProxy(s, _TEST_SCOPE, add_test_flag=True)
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
    store.merge_node("Project", "name", a, {})
    store.merge_node("Technology", "name", b, {})
    store.merge_relation("Project", "name", a, "USES", "Technology", "name", b)
    store.merge_relation("Project", "name", a, "USES", "Technology", "name", b)
    rows = store.get_neighbours("Project", "name", a, hops=1)
    matching = [r for r in rows if r["neighbour"].get("name") == b]
    assert len(matching) == 1


def test_get_neighbours_one_hop(store):
    a = _unique("a")
    b = _unique("b")
    store.merge_node("Project", "name", a, {})
    store.merge_node("Technology", "name", b, {})
    store.merge_relation("Project", "name", a, "USES", "Technology", "name", b)
    rows = store.get_neighbours("Project", "name", a, hops=1)
    assert any(r["neighbour"].get("name") == b for r in rows)


def test_get_neighbours_two_hops_reaches_further(store):
    a = _unique("a")
    b = _unique("b")
    c = _unique("c")
    store.merge_node("Project", "name", a, {})
    store.merge_node("Technology", "name", b, {})
    store.merge_node("Concept", "name", c, {})
    store.merge_relation("Project", "name", a, "USES", "Technology", "name", b)
    store.merge_relation("Technology", "name", b, "APPLIES", "Concept", "name", c)
    one = {r["neighbour"].get("name") for r in store.get_neighbours("Project", "name", a, hops=1)}
    two = {r["neighbour"].get("name") for r in store.get_neighbours("Project", "name", a, hops=2)}
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
    store.merge_node("Project", "name", pname, {})
    store.merge_node("Insight", "title", iname, {"status": "pending"})
    counts = store.count_labels()
    assert counts.get("Project", 0) >= 1
    assert "Insight" not in counts
