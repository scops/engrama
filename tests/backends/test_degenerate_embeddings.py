"""Tests for the degenerate-embedding guard (issue #18).

Covers both layers of the fix:

1. ``engrama.embeddings.health.is_degenerate_vector`` — pure helper, no I/O.
2. ``EngramaEngine.merge_node`` — when the embedder returns a degenerate
   vector, the engine must (a) skip vector storage and (b) flag the
   node ``needs_reindex=True`` so ``engrama reindex`` heals it later.

Runs entirely against an in-memory SQLite store so the SQLite-only CI
job covers it without Neo4j or any embedding service.
"""

from __future__ import annotations

from typing import Any

import pytest

from engrama.backends.sqlite import SqliteGraphStore
from engrama.core.engine import EngramaEngine
from engrama.embeddings.health import is_degenerate_vector

# ----------------------------------------------------------------------
# Unit: is_degenerate_vector
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "vector, expected",
    [
        # Missing or empty payloads from the provider.
        (None, True),
        ([], True),
        # Zero-norm vectors — the exact failure mode reported in #18.
        ([0.0], True),
        ([0.0, 0.0, 0.0, 0.0], True),
        # Sub-epsilon norm: still effectively degenerate (cosine
        # similarity is undefined for these in practice).
        ([1e-15, 1e-15, 1e-15], True),
        # Genuine vectors must NOT be flagged.
        ([1.0, 0.0, 0.0, 0.0], False),
        ([0.5, 0.5, 0.5, 0.5], False),
        ([-0.3, 0.4, -0.5, 0.6], False),
        # Mixed near-zero + one meaningful component: still healthy
        # because the single component lifts the norm well above
        # epsilon.
        ([1e-15, 1e-15, 0.7, 1e-15], False),
    ],
)
def test_is_degenerate_vector(vector, expected):
    assert is_degenerate_vector(vector) is expected


# ----------------------------------------------------------------------
# Integration: EngramaEngine.merge_node ⇄ needs_reindex flag
# ----------------------------------------------------------------------


class _MockEmbedder:
    """Configurable embedder: returns whatever ``response`` is set to.

    ``dimensions`` is non-zero so ``EngramaEngine`` actually engages the
    embed-on-write path.
    """

    dimensions = 4

    def __init__(self, response: list[float] | None = None) -> None:
        self.response = response if response is not None else [0.5, 0.5, 0.5, 0.5]
        self.calls = 0

    def embed(self, text: str) -> list[float] | None:
        self.calls += 1
        return self.response


class _RecordingVectorStore:
    """Stub vector store that records every call to
    ``store_vector_by_key`` so tests can assert it was (or was not)
    invoked.
    """

    dimensions = 4

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, list[float]]] = []

    def store_vector_by_key(
        self,
        label: str,
        key_field: str,
        key_value: str,
        embedding: list[float],
    ) -> bool:
        self.calls.append((label, key_field, key_value, list(embedding)))
        return True


@pytest.fixture()
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "degenerate.db")
    yield s
    s.close()


def _make_engine(
    store: SqliteGraphStore,
    embedder_response: list[float] | None,
) -> tuple[EngramaEngine, _MockEmbedder, _RecordingVectorStore]:
    embedder = _MockEmbedder(embedder_response)
    vector = _RecordingVectorStore()
    engine = EngramaEngine(store, vector_store=vector, embedder=embedder)
    return engine, embedder, vector


def _node_props(store: SqliteGraphStore, label: str, name: str) -> dict[str, Any]:
    n = store.get_node(label, "name", name)
    assert n is not None, f"{label}/{name} not found"
    return n


def test_healthy_embedding_clears_needs_reindex_and_stores_vector(store):
    engine, embedder, vector = _make_engine(store, [0.5, 0.5, 0.5, 0.5])
    engine.merge_node("Project", {"name": "healthy", "description": "real content"})

    props = _node_props(store, "Project", "healthy")
    # Healthy path: vector recorded, flag explicitly False (not just
    # missing — explicit `False` is what tells a later reader the
    # decision was deliberate).
    assert props["needs_reindex"] is False
    assert len(vector.calls) == 1
    assert vector.calls[0][:3] == ("Project", "name", "healthy")


@pytest.mark.parametrize(
    "degenerate_response",
    [
        [],
        [0.0, 0.0, 0.0, 0.0],
        [1e-15, 1e-15, 1e-15, 1e-15],
    ],
    ids=["empty", "zero", "sub-epsilon"],
)
def test_degenerate_embedding_flags_and_skips_vector(store, degenerate_response):
    engine, embedder, vector = _make_engine(store, degenerate_response)
    engine.merge_node(
        "Project",
        {"name": "polluted", "description": "would have been bogus"},
    )

    props = _node_props(store, "Project", "polluted")
    assert props["needs_reindex"] is True
    assert vector.calls == [], (
        "a degenerate vector must not be persisted — it would corrupt "
        "ranking with cosine ≈ 1.0 against every query (issue #18)"
    )


def test_remerge_with_healthy_embedder_clears_the_flag(store):
    """Self-healing: the workaround documented in #18 was re-saving
    via MERGE. With this fix, the re-save not only generates a real
    vector but also flips ``needs_reindex`` back to ``False`` so the
    node leaves the reindex queue.
    """
    # First write: provider is down → degenerate result.
    engine_down, _, vector_down = _make_engine(store, [])
    engine_down.merge_node("Project", {"name": "self-heal", "description": "x"})
    assert _node_props(store, "Project", "self-heal")["needs_reindex"] is True
    assert vector_down.calls == []

    # Second write: provider is back. Same key → MERGE updates the
    # existing node and the flag flips.
    engine_up, _, vector_up = _make_engine(store, [0.1, 0.2, 0.3, 0.4])
    engine_up.merge_node("Project", {"name": "self-heal", "description": "x"})
    assert _node_props(store, "Project", "self-heal")["needs_reindex"] is False
    assert len(vector_up.calls) == 1


def test_list_nodes_for_embedding_surfaces_needs_reindex(store):
    """``engrama reindex`` (force=False) must pick up nodes that were
    flagged at write-time — that's how the batch repair documented in
    #18 actually heals them.
    """
    engine, _, _ = _make_engine(store, [])
    engine.merge_node("Project", {"name": "flagged", "description": "x"})
    # Sanity: a healthy node coexists.
    engine_h, _, _ = _make_engine(store, [0.5, 0.5, 0.5, 0.5])
    engine_h.merge_node("Project", {"name": "ok", "description": "y"})

    pending = {r["props"]["name"] for r in store.list_nodes_for_embedding(force=False)}
    assert "flagged" in pending
