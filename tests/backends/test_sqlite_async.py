"""Tests for ``SqliteAsyncStore``.

These tests assert the **rich async contract** that mirrors
``Neo4jAsyncStore`` (e.g. ``merge_node`` returns ``{"node": ...,
"created": ...}``, neighbours come back as ``{label, name, via,
properties}``).  Cross-backend equivalence lives in the parameterised
``tests/contracts/test_async_graphstore_contract.py`` suite — these
tests focus on SQLite-specific edges (vector ops, name aliases).

Spec 001 fail-closed migration: a ``_ScopedAsyncStoreProxy`` wraps the
real ``SqliteAsyncStore`` so writes auto-stamp the test scope and
scoped reads auto-forward it — keeping each test focused on the
backend contract rather than scoping plumbing.
"""

from __future__ import annotations

from typing import Any

import pytest

from engrama.backends.sqlite import SqliteAsyncStore
from engrama.core.scope import MemoryScope

_TEST_SCOPE = MemoryScope(org_id="test-sqlite-async", user_id="test-sqlite-async")
_SCOPE_PROPS = {"org_id": "test-sqlite-async", "user_id": "test-sqlite-async"}


class _ScopedAsyncStoreProxy:
    """Auto-stamp scope on writes; auto-forward scope to scoped reads.

    Mirrors ``tests/backends/test_sqlite.py``'s sync proxy. Methods not
    enumerated here pass through unchanged.
    """

    def __init__(self, inner: SqliteAsyncStore, scope: MemoryScope) -> None:
        self._inner = inner
        self._scope = scope

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    async def close(self) -> None:
        await self._inner.close()

    # ---- writes ------------------------------------------------------

    async def merge_node(self, label, key_field, key_value, properties, embedding=None):
        properties = {**_SCOPE_PROPS, **properties}
        return await self._inner.merge_node(label, key_field, key_value, properties, embedding)

    async def merge_relation(
        self, from_label, from_key, from_value, rel_type, to_label, to_key, to_value, scope=None
    ):
        return await self._inner.merge_relation(
            from_label,
            from_key,
            from_value,
            rel_type,
            to_label,
            to_key,
            to_value,
            scope=scope or self._scope,
        )

    # ---- scoped reads ------------------------------------------------

    async def get_neighbours(self, label, key_field, key_value, hops=1, limit=50, scope=None):
        return await self._inner.get_neighbours(
            label, key_field, key_value, hops=hops, limit=limit, scope=scope or self._scope
        )

    async def get_node_with_neighbours(self, label, key_field, key_value, hops=1, scope=None):
        return await self._inner.get_node_with_neighbours(
            label, key_field, key_value, hops=hops, scope=scope or self._scope
        )

    async def fulltext_search(self, query, limit=10, scope=None):
        return await self._inner.fulltext_search(query, limit=limit, scope=scope or self._scope)

    async def count_labels(self, scope=None):
        return await self._inner.count_labels(scope=scope or self._scope)

    async def lookup_node_label(self, name, scope=None):
        return await self._inner.lookup_node_label(name, scope=scope or self._scope)

    async def list_existing_nodes(self, limit=200, scope=None):
        return await self._inner.list_existing_nodes(limit=limit, scope=scope or self._scope)

    async def get_dismissed_titles(self, scope=None):
        return await self._inner.get_dismissed_titles(scope=scope or self._scope)

    async def get_approved_titles(self, scope=None):
        return await self._inner.get_approved_titles(scope=scope or self._scope)

    async def get_pending_insights(self, limit=10, scope=None):
        return await self._inner.get_pending_insights(limit=limit, scope=scope or self._scope)

    async def get_insight_by_title(self, title, scope=None):
        return await self._inner.get_insight_by_title(title, scope=scope or self._scope)

    async def find_insight_by_source_query(self, source_query, statuses=None, scope=None):
        return await self._inner.find_insight_by_source_query(
            source_query, statuses=statuses, scope=scope or self._scope
        )

    async def search_similar(self, query_embedding, limit=10, scope=None):
        return await self._inner.search_similar(
            query_embedding, limit=limit, scope=scope or self._scope
        )


@pytest.fixture()
async def store(tmp_path):
    s = SqliteAsyncStore(tmp_path / "async.db", vector_dimensions=4)
    proxy = _ScopedAsyncStoreProxy(s, _TEST_SCOPE)
    yield proxy
    await s.close()


# ----------------------------------------------------------------------
# Properties
# ----------------------------------------------------------------------


async def test_dimensions_property(store):
    assert store.dimensions == 4


# ----------------------------------------------------------------------
# Node + relationship contract (rich shape)
# ----------------------------------------------------------------------


async def test_merge_node_returns_dict_with_node_and_created(store):
    out = await store.merge_node("Project", "name", "p", {"description": "x"})
    assert isinstance(out, dict)
    assert "node" in out and "created" in out
    assert out["created"] is True
    assert out["node"]["name"] == "p"
    assert out["node"]["description"] == "x"
    # Internal markers must not leak into the async response.
    assert "_id" not in out["node"]
    assert "_labels" not in out["node"]


async def test_merge_node_second_call_marks_not_created(store):
    await store.merge_node("Project", "name", "p", {"description": "x"})
    out = await store.merge_node("Project", "name", "p", {"description": "y"})
    assert out["created"] is False
    assert out["node"]["description"] == "y"


async def test_merge_relation_returns_dict_with_obsidian_path_field(store):
    await store.merge_node(
        "Project",
        "name",
        "a",
        {"obsidian_path": "a.md"},
    )
    await store.merge_node("Technology", "name", "py", {})
    out = await store.merge_relation(
        "Project",
        "name",
        "a",
        "USES",
        "Technology",
        "name",
        "py",
    )
    assert isinstance(out, dict)
    assert out["rel_type"] == "USES"
    assert out["from_name"] == "a"
    assert out["to_name"] == "py"
    assert out["from_obsidian_path"] == "a.md"


async def test_merge_relation_returns_empty_dict_on_missing_endpoint(store):
    out = await store.merge_relation(
        "Project",
        "name",
        "ghost",
        "USES",
        "Technology",
        "name",
        "py",
    )
    assert out == {}


async def test_get_neighbours_returns_label_name_via_properties_shape(store):
    await store.merge_node("Project", "name", "a", {})
    await store.merge_node("Technology", "name", "py", {"summary": "lang"})
    await store.merge_relation(
        "Project",
        "name",
        "a",
        "USES",
        "Technology",
        "name",
        "py",
    )
    out = await store.get_neighbours("Project", "name", "a", hops=1)
    assert len(out) == 1
    n = out[0]
    assert n["label"] == "Technology"
    assert n["name"] == "py"
    assert n["via"] == ["USES"]
    assert n["properties"]["summary"] == "lang"
    # Internal/timestamp fields must be stripped from neighbour properties.
    assert "_id" not in n["properties"]
    assert "_labels" not in n["properties"]
    assert "created_at" not in n["properties"]


# ----------------------------------------------------------------------
# Insight lifecycle (note: alias for dismissed titles)
# ----------------------------------------------------------------------


async def test_get_dismissed_titles_alias(store):
    await store.merge_node(
        "Insight",
        "title",
        "i1",
        {
            "body": "x",
            "confidence": 0.9,
            "status": "pending",
        },
    )
    assert await store.get_dismissed_titles() == set()
    await store.update_insight_status("i1", "dismissed")
    assert await store.get_dismissed_titles() == {"i1"}


async def test_pending_insight_lifecycle(store):
    await store.merge_node(
        "Insight",
        "title",
        "i1",
        {
            "body": "x",
            "confidence": 0.9,
            "status": "pending",
        },
    )
    pending = await store.get_pending_insights()
    assert pending and pending[0]["title"] == "i1"
    assert await store.update_insight_status("i1", "approved") is True


# ----------------------------------------------------------------------
# Vector ops via the async wrapper (renamed methods)
# ----------------------------------------------------------------------


async def test_store_embedding_and_search_similar(store):
    await store.merge_node(
        "Project",
        "name",
        "p",
        {
            "summary": "demo summary",
            "tags": ["demo", "vector"],
        },
    )
    assert (
        await store.store_embedding(
            "Project",
            "name",
            "p",
            [1.0, 0.0, 0.0, 0.0],
        )
        is True
    )
    assert await store.count_embeddings() == 1
    out = await store.search_similar([1.0, 0.0, 0.0, 0.0], limit=1)
    assert out
    # Async contract: ``name`` (not ``key``) and a label field.
    assert out[0]["name"] == "p"
    assert out[0]["label"] == "Project"
    assert "node_id" in out[0] and "score" in out[0]
    # Enrichment fields must be projected so the hybrid scorer can
    # populate summary/tags for pure-semantic hits without a second
    # round trip.
    assert out[0]["summary"] == "demo summary"
    assert out[0]["tags"] == ["demo", "vector"]


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------


async def test_health_check_async_shape(store):
    h = await store.health_check()
    assert h["status"] == "ok"
    assert h["backend"] == "sqlite-async"
