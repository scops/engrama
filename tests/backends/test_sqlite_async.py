"""Tests for the async SQLite store (thin wrapper over the sync stores)."""

from __future__ import annotations

import pytest

from engrama.backends.sqlite import SqliteAsyncStore


@pytest.fixture()
async def store(tmp_path):
    s = SqliteAsyncStore(tmp_path / "async.db", vector_dimensions=4)
    yield s
    await s.close()


# ----------------------------------------------------------------------
# Sync attributes
# ----------------------------------------------------------------------


async def test_dimensions_property(store):
    assert store.dimensions == 4


# ----------------------------------------------------------------------
# Async delegation
# ----------------------------------------------------------------------


async def test_merge_and_get_node(store):
    out = await store.merge_node("Project", "name", "p", {"description": "x"})
    assert out[0]["n"]["name"] == "p"
    n = await store.get_node("Project", "name", "p")
    assert n["description"] == "x"


async def test_merge_relation_and_neighbours(store):
    await store.merge_node("Project", "name", "a", {})
    await store.merge_node("Technology", "name", "py", {})
    out = await store.merge_relation("Project", "name", "a", "USES", "Technology", "name", "py")
    assert out and out[0]["rel_type"] == "USES"
    rows = await store.get_neighbours("Project", "name", "a", hops=1)
    assert rows and rows[0]["neighbour"]["name"] == "py"


async def test_fulltext_search(store):
    await store.merge_node("Project", "name", "p", {"description": "graph database memory"})
    out = await store.fulltext_search("memory")
    assert any(r["name"] == "p" for r in out)


async def test_insight_lifecycle(store):
    await store.merge_node("Insight", "title", "i1", {
        "body": "x", "confidence": 0.9, "status": "pending",
    })
    pending = await store.get_pending_insights()
    assert pending and pending[0]["title"] == "i1"
    assert await store.update_insight_status("i1", "approved") is True


async def test_detect_shared_technology_via_async(store):
    await store.merge_node("Project",    "name", "alpha", {})
    await store.merge_node("Project",    "name", "beta",  {})
    await store.merge_node("Technology", "name", "py",    {})
    await store.merge_relation("Project", "name", "alpha", "USES", "Technology", "name", "py")
    await store.merge_relation("Project", "name", "beta",  "USES", "Technology", "name", "py")
    out = await store.detect_shared_technology()
    assert any({r["entity_a"], r["entity_b"]} == {"alpha", "beta"} for r in out)


async def test_health_check(store):
    h = await store.health_check()
    assert h["ok"] is True
    assert h["backend"] == "sqlite"


# ----------------------------------------------------------------------
# Vector ops via the async wrapper
# ----------------------------------------------------------------------


async def test_async_vector_round_trip(store):
    n = await store.merge_node("Project", "name", "p", {})
    nid = n[0]["n"]["_id"]
    await store.store_vectors([(nid, [1.0, 0.0, 0.0, 0.0])])
    assert await store.count() == 1
    out = await store.search_vectors([1.0, 0.0, 0.0, 0.0], limit=1)
    assert out and out[0]["key"] == "p"


# ----------------------------------------------------------------------
# Failure surface
# ----------------------------------------------------------------------


async def test_unknown_attribute_raises_attribute_error(store):
    with pytest.raises(AttributeError):
        await store.no_such_method_on_either_store()
