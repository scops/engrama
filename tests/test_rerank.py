"""
Tests for the pure hybrid reranking primitives (spec 002-hybrid-reranking).

Scaffold only: shared imports + fixtures for a small in-tenant candidate
graph. The behavioural cases hang off these fixtures in later tasks —
``rrf_fuse`` units (T006), cohesion units (T011), anchor units (T012), and
the tenancy isolation cases (T013). No assertions live here yet (P7: the
failing tests are authored in their own tasks).

All math under test is pure and backend-agnostic, so these are unit tests
with plain Python fixtures — no store, no Neo4j, no embedder. The SQLite
path is the local test backend per the constitution; nothing here touches
the shared production graph.
"""

from __future__ import annotations

import pytest

from engrama.core.rerank import (  # noqa: F401  (re-exported for later test tasks)
    DEFAULT_RRF_K,
    QueryAnchor,
    RrfFusion,
    graph_distance_scores,
    resolve_anchor,
    rrf_fuse,
)
from engrama.core.scope import MemoryScope

# ---------------------------------------------------------------------------
# Tenancy fixtures — one in-tenant scope plus a foreign scope whose nodes
# must never leak into cohesion or anchor resolution (T013, RG-4).
# ---------------------------------------------------------------------------


@pytest.fixture
def scope() -> MemoryScope:
    """The calling tenant for every in-tenant fixture below."""
    return MemoryScope(org_id="org-a", user_id="user-a")


@pytest.fixture
def other_scope() -> MemoryScope:
    """A foreign tenant — its nodes must stay invisible to ``scope``."""
    return MemoryScope(org_id="org-b", user_id="user-b")


# ---------------------------------------------------------------------------
# Candidate-graph fixture
#
# A deliberately mixed window so later tasks can assert the contract:
#   * ``cluster-*`` form a connected triangle (cohesion should lift them);
#   * ``isolated`` has no in-tenant neighbour (cohesion no-op);
#   * ``anchor-node`` is what the query "anchor" resolves to;
#   * channel ranks are split so RRF has something non-trivial to fuse.
#
# The neighbour map is already scope-filtered (as a real scoped store would
# return it): the cross-tenant node never appears as anyone's neighbour.
# ---------------------------------------------------------------------------

# Best-first ranked names per channel (index 0 == rank 1).
VECTOR_LIST: list[str] = ["cluster-a", "anchor-node", "cluster-b", "isolated"]
FULLTEXT_LIST: list[str] = ["cluster-b", "cluster-c", "anchor-node"]

# In-tenant adjacency within the candidate window (undirected, scope-filtered).
NEIGHBOURS: dict[str, list[str]] = {
    "cluster-a": ["cluster-b", "cluster-c"],
    "cluster-b": ["cluster-a", "cluster-c", "anchor-node"],
    "cluster-c": ["cluster-a", "cluster-b"],
    "anchor-node": ["cluster-b"],
    "isolated": [],
}

# A foreign-tenant node that an unscoped store *would* have returned as a
# neighbour of ``cluster-a``; the scoped path excludes it. Kept here so the
# isolation test (T013) can prove it never influences the score.
CROSS_TENANT_NEIGHBOUR: str = "org-b-secret"


@pytest.fixture
def candidate_graph() -> dict[str, object]:
    """A small in-tenant candidate graph shared across reranking tests.

    Returns the channel rank lists and the scope-filtered neighbour map as
    a single bundle so behavioural tasks can pull exactly what they need.
    """
    return {
        "vector_list": list(VECTOR_LIST),
        "fulltext_list": list(FULLTEXT_LIST),
        "neighbours": {k: list(v) for k, v in NEIGHBOURS.items()},
    }


# ===========================================================================
# T006 — Unit tests for ``rrf_fuse`` (US1). Written first; MUST FAIL until
# T008 implements the function (currently raises NotImplementedError).
#
# Normalization contract: RRF raw weights are min-max scaled to [0, 1]
# (matching the existing per-channel convention in ``search._merge``), with
# an all-equal window collapsing to 1.0 — never 0/0 NaN.
# ===========================================================================


def _best_rank(c: RrfFusion) -> int:
    """The candidate's best (lowest) rank across channels — the documented
    deterministic tie-break key after ``rrf_score`` (data-model.md)."""
    ranks = [r for r in (c.vector_rank, c.fulltext_rank) if r is not None]
    return min(ranks)


def test_rrf_basic_math_and_ranks():
    """RRF raw weight = Σ 1/(k+rank); ranks recorded per channel; min-max norm.

    vector = [A, B, C] (ranks 1,2,3), fulltext = [B, A] (ranks 1,2), k=60.
        A = 1/61 + 1/62   (vector#1, fulltext#2)
        B = 1/62 + 1/61   (vector#2, fulltext#1)  -> ties A
        C = 1/63          (vector#3, fulltext absent)
    min-max ⇒ A=B=1.0 (max), C=0.0 (min).
    """
    out = rrf_fuse(["A", "B", "C"], ["B", "A"], k=60)

    assert set(out) == {"A", "B", "C"}
    # Per-channel ranks (1-based; None when absent).
    assert (out["A"].vector_rank, out["A"].fulltext_rank) == (1, 2)
    assert (out["B"].vector_rank, out["B"].fulltext_rank) == (2, 1)
    assert (out["C"].vector_rank, out["C"].fulltext_rank) == (3, None)
    # Symmetric raw weights ⇒ A and B tie at the normalized max; C is the min.
    assert out["A"].rrf_score == pytest.approx(1.0)
    assert out["B"].rrf_score == pytest.approx(1.0)
    assert out["C"].rrf_score == pytest.approx(0.0)


def test_rrf_scale_invariant_uses_rank_only():
    """RG-1: fusion consumes only rank order, never raw magnitudes.

    Two channels with the *same ordering* must yield identical fused output
    regardless of any underlying score scale (scores never reach the
    function). A higher-ranked item always outranks a lower-ranked one.
    """
    a = rrf_fuse(["X", "Y", "Z"], ["Y", "X"], k=60)
    b = rrf_fuse(["X", "Y", "Z"], ["Y", "X"], k=60)
    assert {n: a[n].rrf_score for n in a} == {n: b[n].rrf_score for n in b}
    # X (vector#1, fulltext#2) outranks Z (vector#3 only).
    assert a["X"].rrf_score > a["Z"].rrf_score


def test_rrf_single_channel_fallback():
    """RG-2: one empty channel ⇒ the other channel's order, no error."""
    out = rrf_fuse(["A", "B", "C"], [], k=60)

    assert set(out) == {"A", "B", "C"}
    # Absent channel ⇒ that rank is None for every candidate.
    assert all(c.fulltext_rank is None for c in out.values())
    assert (out["A"].vector_rank, out["B"].vector_rank, out["C"].vector_rank) == (1, 2, 3)
    # Order follows the single channel: A (rank1) > B > C (rank3).
    assert out["A"].rrf_score > out["B"].rrf_score > out["C"].rrf_score
    assert out["A"].rrf_score == pytest.approx(1.0)
    assert out["C"].rrf_score == pytest.approx(0.0)


def test_rrf_normalization_bounds():
    """Every rrf_score lands in [0,1]; max==1.0 and min==0.0 for a ≥2-item window."""
    out = rrf_fuse(["A", "B", "C", "D"], ["D", "C"], k=60)
    scores = [c.rrf_score for c in out.values()]
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert max(scores) == pytest.approx(1.0)
    assert min(scores) == pytest.approx(0.0)


def test_rrf_single_result_no_nan():
    """A single candidate normalizes to 1.0 (all-equal guard), never NaN."""
    out = rrf_fuse(["solo"], [], k=60)
    assert set(out) == {"solo"}
    score = out["solo"].rrf_score
    assert score == score  # not NaN
    assert score == pytest.approx(1.0)
    assert out["solo"].vector_rank == 1
    assert out["solo"].fulltext_rank is None


def test_rrf_tie_broken_by_rank_then_name():
    """RG-3: a score tie is broken deterministically by best rank, then name.

    A (vector#1, fulltext#2) and B (vector#2, fulltext#1) tie on rrf_score;
    both have best_rank 1, so the tie resolves by lexicographic name ⇒ A, B.
    """
    out = rrf_fuse(["A", "B"], ["B", "A"], k=60)
    assert out["A"].rrf_score == pytest.approx(out["B"].rrf_score)

    ordered = sorted(
        out.values(),
        key=lambda c: (-c.rrf_score, _best_rank(c), c.name),
    )
    assert [c.name for c in ordered] == ["A", "B"]


def test_rrf_both_empty_returns_empty():
    """No candidates in either channel ⇒ empty mapping, no error."""
    assert rrf_fuse([], [], k=60) == {}
