"""
Async ``GraphStore`` contract test suite.

Every backend that the MCP server can drive — i.e. every store returned
by :func:`engrama.backends.create_async_stores` — must pass these
tests.  They define the rich response shapes the MCP tool handlers
depend on (``merge_node`` returns ``{"node": ..., "created": ...}``,
neighbours come back as ``{label, name, via, properties}``, etc.).

Parameterised over: ``sqlite-async``, ``neo4j-async`` (skipped if
``NEO4J_PASSWORD`` is unset).  The Neo4j fixture tags every test node
with ``test=True`` so the cleanup pass at fixture teardown removes
them.
"""

from __future__ import annotations

import os
import uuid

import pytest


def _unique(prefix: str = "act") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(params=["sqlite-async", "neo4j-async"])
async def store(request, tmp_path):
    if request.param == "sqlite-async":
        from engrama.backends.sqlite import SqliteAsyncStore

        s = SqliteAsyncStore(tmp_path / "async-contract.db", vector_dimensions=0)
        yield s
        await s.close()
        return

    if request.param == "neo4j-async":
        if not os.getenv("NEO4J_PASSWORD"):
            pytest.skip("Neo4j not configured (set NEO4J_PASSWORD to run)")
        from neo4j import AsyncGraphDatabase

        from engrama.backends.neo4j.async_store import Neo4jAsyncStore

        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USERNAME", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "")
        driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        s = Neo4jAsyncStore(driver, database=os.getenv("NEO4J_DATABASE", "neo4j"))

        # Tag every test node with test=True for cleanup.
        original_merge = s.merge_node

        async def _tagged_merge(label, key_field, key_value, properties, embedding=None):
            tagged = dict(properties)
            tagged.setdefault("test", True)
            return await original_merge(
                label, key_field, key_value, tagged, embedding=embedding,
            )

        s.merge_node = _tagged_merge  # type: ignore[method-assign]
        yield s
        try:
            await driver.execute_query(
                "MATCH (n) WHERE n.test = true DETACH DELETE n",
                database_=s._database,  # type: ignore[attr-defined]
            )
        except Exception:
            pass
        await driver.close()
        return

    raise ValueError(f"unknown async backend {request.param!r}")


# ----------------------------------------------------------------------
# merge_node contract
# ----------------------------------------------------------------------


async def test_merge_node_returns_node_and_created_dict(store):
    name = _unique("p")
    out = await store.merge_node("Project", "name", name, {"description": "x"})
    assert isinstance(out, dict)
    assert "node" in out
    assert "created" in out
    assert out["created"] is True
    assert isinstance(out["node"], dict)
    assert out["node"]["name"] == name
    assert out["node"]["description"] == "x"
    # Backend-internal markers must not leak.
    assert "_id" not in out["node"]
    assert "_labels" not in out["node"]


async def test_merge_node_second_write_is_match(store):
    name = _unique("p")
    await store.merge_node("Project", "name", name, {"status": "active"})
    out = await store.merge_node("Project", "name", name, {"status": "paused"})
    assert out["created"] is False
    assert out["node"]["status"] == "paused"


async def test_get_node_returns_props_or_none(store):
    name = _unique("p")
    await store.merge_node("Project", "name", name, {"description": "demo"})
    n = await store.get_node("Project", "name", name)
    assert n is not None
    assert n["description"] == "demo"
    assert await store.get_node("Project", "name", _unique("missing")) is None


# ----------------------------------------------------------------------
# merge_relation contract
# ----------------------------------------------------------------------


async def test_merge_relation_returns_rich_dict(store):
    a, b = _unique("a"), _unique("b")
    await store.merge_node("Project", "name", a, {})
    await store.merge_node("Technology", "name", b, {})
    out = await store.merge_relation(
        "Project", "name", a, "USES", "Technology", "name", b,
    )
    assert isinstance(out, dict)
    assert out["rel_type"] == "USES"
    assert out["from_name"] == a
    assert out["to_name"] == b
    # Path field must be present (None or a string), so MCP can call .get on it.
    assert "from_obsidian_path" in out


async def test_merge_relation_empty_dict_when_endpoint_missing(store):
    out = await store.merge_relation(
        "Project", "name", _unique("ghost"),
        "USES",
        "Technology", "name", _unique("ghost"),
    )
    assert out == {}


# ----------------------------------------------------------------------
# get_neighbours contract
# ----------------------------------------------------------------------


async def test_get_neighbours_uses_label_name_via_properties(store):
    a, b = _unique("a"), _unique("b")
    await store.merge_node("Project", "name", a, {})
    await store.merge_node("Technology", "name", b, {"summary": "lang"})
    await store.merge_relation(
        "Project", "name", a, "USES", "Technology", "name", b,
    )
    out = await store.get_neighbours("Project", "name", a, hops=1)
    matches = [n for n in out if n["name"] == b]
    assert len(matches) == 1
    n = matches[0]
    assert n["label"] == "Technology"
    assert n["via"] == ["USES"]
    assert n["properties"].get("summary") == "lang"
    # Properties must not include backend-internal or noise fields.
    for forbidden in ("_id", "_labels", "created_at", "updated_at", "details", "embedding"):
        assert forbidden not in n["properties"]


async def test_get_node_with_neighbours_shape(store):
    a, b = _unique("a"), _unique("b")
    await store.merge_node("Project", "name", a, {"description": "root"})
    await store.merge_node("Technology", "name", b, {})
    await store.merge_relation(
        "Project", "name", a, "USES", "Technology", "name", b,
    )
    out = await store.get_node_with_neighbours("Project", "name", a, hops=1)
    assert out is not None
    assert isinstance(out["node"], dict)
    assert out["node"].get("description") == "root"
    assert isinstance(out["neighbours"], list)
    names = {n["name"] for n in out["neighbours"]}
    assert b in names


# ----------------------------------------------------------------------
# fulltext_search contract
# ----------------------------------------------------------------------


async def test_fulltext_search_matches_description(store):
    name = _unique("ftsdesc")
    needle = f"asyncneedle{uuid.uuid4().hex[:6]}"
    await store.merge_node(
        "Project", "name", name, {"description": f"a {needle} marker"},
    )
    out = await store.fulltext_search(needle)
    assert any(r["name"] == name for r in out)


# ----------------------------------------------------------------------
# Insight contract
# ----------------------------------------------------------------------


async def test_insight_lifecycle_and_dismissed_titles_alias(store):
    title = _unique("ins")
    await store.merge_node("Insight", "title", title, {
        "body": "x", "confidence": 0.9, "status": "pending",
    })
    pending = await store.get_pending_insights()
    assert any(p["title"] == title for p in pending)
    assert await store.update_insight_status(title, "dismissed") is True
    assert title in await store.get_dismissed_titles()


async def test_get_approved_titles(store):
    """Approved insights surface in ``get_approved_titles`` (NOT in
    dismissed). Reflect uses this to skip patterns the user has already
    accepted, avoiding silent re-pinning to ``status='pending'`` on a
    re-run.
    """
    title = _unique("appins")
    await store.merge_node("Insight", "title", title, {
        "body": "x", "confidence": 0.9, "status": "pending",
    })
    assert title not in await store.get_approved_titles()
    await store.update_insight_status(title, "approved")
    assert title in await store.get_approved_titles()
    assert title not in await store.get_dismissed_titles()


# ----------------------------------------------------------------------
# Lookup
# ----------------------------------------------------------------------


async def test_lookup_node_label_returns_str_or_none(store):
    name = _unique("Mixed")
    await store.merge_node("Project", "name", name, {})
    assert await store.lookup_node_label(name.lower()) == "Project"
    assert await store.lookup_node_label(_unique("missing")) is None


# ----------------------------------------------------------------------
# Counts
# ----------------------------------------------------------------------


async def test_count_labels_excludes_insights(store):
    pname = _unique("p")
    iname = _unique("i")
    await store.merge_node("Project", "name", pname, {})
    await store.merge_node("Insight", "title", iname, {"status": "pending"})
    counts = await store.count_labels()
    assert counts.get("Project", 0) >= 1
    assert "Insight" not in counts


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------


async def test_health_check_returns_status_and_backend(store):
    h = await store.health_check()
    assert "status" in h
    assert "backend" in h
