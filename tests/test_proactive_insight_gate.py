"""Proactive pending-Insight hint: the search hint and surface_insights must
read the SAME pending queue, and the search hint gates each entry by pure
cosine relevance (τ) and confidence (κ).

Covers the cosine helper, the gate (`_related_pending_insights`) incl. thresholds,
env overrides and short-circuits, and the store query `get_pending_insight_vectors`
(scope + pending-only + skip-no-vector + engrama_id) on the real SQLite backend.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engrama.adapters.mcp.server import (
    _cosine,
    _related_pending_insights,
)
from engrama.backends.sqlite.store import SqliteGraphStore
from engrama.backends.sqlite.vector import SqliteVecStore
from engrama.core.scope import MemoryScope

_SCOPE = MemoryScope(org_id="o", user_id="u")
_OTHER = MemoryScope(org_id="o2", user_id="u2")


# --- _cosine (pure) ---------------------------------------------------------


def test_cosine_basic() -> None:
    assert _cosine([1, 0, 0, 0], [1, 0, 0, 0]) == pytest.approx(1.0)
    assert _cosine([1, 0, 0, 0], [0, 1, 0, 0]) == pytest.approx(0.0)
    assert _cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)


def test_cosine_degenerate_returns_zero() -> None:
    assert _cosine([], [1, 2]) == 0.0
    assert _cosine([1, 2], [1, 2, 3]) == 0.0
    assert _cosine([0, 0], [1, 1]) == 0.0


# --- gate logic with a fake async store -------------------------------------


class _FakeStore:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def get_pending_insight_vectors(self, limit=10, scope=None):
        return self._rows[:limit]


_ROWS = [
    {"engrama_id": "a", "title": "A", "body": "x", "confidence": 1.0, "embedding": [1, 0, 0, 0]},
    {"engrama_id": "b", "title": "B", "body": "x", "confidence": 1.0, "embedding": [0, 1, 0, 0]},
    {"engrama_id": "c", "title": "C", "body": "x", "confidence": 0.3, "embedding": [1, 0, 0, 0]},
]


def test_gate_keeps_only_relevant_and_confident() -> None:
    matched = asyncio.run(_related_pending_insights(_FakeStore(_ROWS), _SCOPE, [1.0, 0, 0, 0]))
    # A: cosine 1.0, conf 1.0 → kept. B: cosine 0 → cut. C: conf 0.3 < κ → cut.
    assert [m["title"] for m in matched] == ["A"]
    assert matched[0]["engrama_id"] == "a"
    assert matched[0]["score"] == pytest.approx(1.0)


def test_gate_short_circuits_without_query_vector() -> None:
    assert asyncio.run(_related_pending_insights(_FakeStore(_ROWS), _SCOPE, None)) == []


def test_gate_short_circuits_on_empty_queue() -> None:
    assert asyncio.run(_related_pending_insights(_FakeStore([]), _SCOPE, [1.0, 0, 0, 0])) == []


def test_gate_tau_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGRAMA_INSIGHT_HINT_TAU", "1.01")  # nothing can reach it
    assert asyncio.run(_related_pending_insights(_FakeStore(_ROWS), _SCOPE, [1.0, 0, 0, 0])) == []


def test_gate_kappa_override_lets_low_confidence_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGRAMA_INSIGHT_HINT_KAPPA", "0.0")
    matched = asyncio.run(_related_pending_insights(_FakeStore(_ROWS), _SCOPE, [1.0, 0, 0, 0]))
    # Now C (conf 0.3, cosine 1.0) also passes; B still cut on cosine.
    assert sorted(m["title"] for m in matched) == ["A", "C"]


# --- get_pending_insight_vectors on the real SQLite backend -----------------


@pytest.fixture()
def store(tmp_path: Path):
    db = tmp_path / "insights.db"
    s = SqliteGraphStore(str(db))
    vec = SqliteVecStore(s._conn, dimensions=4)
    vec.ensure_index()
    return s, vec


def _add_insight(s, vec, title, *, status, confidence, scope, embedding=None):
    s.merge_node(
        "Insight",
        "title",
        title,
        {
            "title": title,
            "body": f"body of {title}",
            "status": status,
            "confidence": confidence,
            "engrama_id": f"id-{title}",
            "org_id": scope.org_id,
            "user_id": scope.user_id,
        },
    )
    if embedding is not None:
        row = s._conn.execute(
            "SELECT id FROM nodes WHERE label='Insight' AND key_value=?", (title,)
        ).fetchone()
        vec.store_vectors([(str(row["id"]), embedding)])


def test_pending_insight_vectors_scope_and_status_and_vector(store) -> None:
    s, vec = store
    _add_insight(
        s, vec, "keep", status="pending", confidence=0.9, scope=_SCOPE, embedding=[1, 0, 0, 0]
    )
    _add_insight(
        s,
        vec,
        "approved",
        status="approved",
        confidence=0.9,
        scope=_SCOPE,
        embedding=[1, 0, 0, 0],
    )  # not pending → excluded
    _add_insight(s, vec, "novec", status="pending", confidence=0.9, scope=_SCOPE)  # no vector
    _add_insight(
        s, vec, "other", status="pending", confidence=0.9, scope=_OTHER, embedding=[1, 0, 0, 0]
    )  # other tenant → excluded

    rows = s.get_pending_insight_vectors(scope=_SCOPE)
    titles = {r["title"] for r in rows}
    assert titles == {"keep"}  # only pending + in-scope + has vector
    assert rows[0]["engrama_id"] == "id-keep"
    assert rows[0]["embedding"] == [1.0, 0.0, 0.0, 0.0]


def test_pending_insight_vectors_fail_closed_without_scope(store) -> None:
    s, vec = store
    _add_insight(
        s, vec, "keep", status="pending", confidence=0.9, scope=_SCOPE, embedding=[1, 0, 0, 0]
    )
    assert s.get_pending_insight_vectors(scope=None) == []
