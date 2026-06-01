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


# ===========================================================================
# T011 — Unit tests for cohesion in ``graph_distance_scores`` (US2). Written
# first; MUST FAIL until T015 (currently raises NotImplementedError).
#
# Contract pinned here: cohesion is relevance-weighted over the candidate
# window only, per-hop decay = cohesion_decay**(dist-1), bounded by `hops`
# and `fanout_cap`, then divide-by-max normalized (isolated node ⇒ 0). The
# final score is clamp01(cohesion_norm + anchor_term).
# ===========================================================================


def test_cohesion_clustered_beats_isolated():
    """A connected cluster outranks an isolated node of equal relevance."""
    cands = ["A", "B", "C", "D"]
    rrf = {"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0}
    nbr = {"A": ["B", "C"], "B": ["A", "C"], "C": ["A", "B"], "D": []}
    scores = graph_distance_scores(cands, rrf, nbr, hops=2, cohesion_decay=0.5, fanout_cap=64)
    assert scores["A"] > scores["D"]
    assert scores["D"] == pytest.approx(0.0)  # no neighbour ⇒ zero cohesion


def test_cohesion_per_hop_decay():
    """A 2-hop neighbour contributes ``decay`` of a 1-hop one.

    Chain A–B–C, all rrf 1.0, hops=2, decay=0.5 (divide-by-max norm):
        cohesion(A) = B(d1)·1 + C(d2)·0.5 = 1.5
        cohesion(B) = A(d1) + C(d1)       = 2.0   (max)
        cohesion(C) = B(d1) + A(d2)·0.5   = 1.5
    ⇒ A=C=0.75, B=1.0. A<B proves the 2-hop hop was decayed (else A=B=1.0).
    """
    cands = ["A", "B", "C"]
    rrf = {"A": 1.0, "B": 1.0, "C": 1.0}
    nbr = {"A": ["B"], "B": ["A", "C"], "C": ["B"]}
    scores = graph_distance_scores(cands, rrf, nbr, hops=2, cohesion_decay=0.5, fanout_cap=64)
    assert scores["B"] == pytest.approx(1.0)
    assert scores["A"] == pytest.approx(0.75)
    assert scores["C"] == pytest.approx(0.75)


def test_cohesion_no_neighbour_is_noop():
    """An isolated candidate scores exactly 0 (no error, no NaN)."""
    scores = graph_distance_scores(
        ["solo"], {"solo": 1.0}, {"solo": []}, hops=2, cohesion_decay=0.5, fanout_cap=64
    )
    assert scores["solo"] == pytest.approx(0.0)


def test_cohesion_normalized_0_1():
    """Every cohesion score lands in [0, 1]."""
    cands = ["A", "B", "C", "D"]
    rrf = {"A": 0.9, "B": 0.5, "C": 0.3, "D": 0.1}
    nbr = {"A": ["B"], "B": ["A", "C"], "C": ["B", "D"], "D": ["C"]}
    scores = graph_distance_scores(cands, rrf, nbr, hops=2, cohesion_decay=0.5, fanout_cap=64)
    assert all(0.0 <= s <= 1.0 for s in scores.values())


def test_cohesion_bounded_by_hops():
    """Nodes beyond ``hops`` do not contribute.

    Chain A–B–C–D with hops=1: A sees only B (d1); C and D are out of range.
    cohesion(A)=B=1.0; max cohesion is the 2-neighbour middles (B,C)=2.0,
    so A normalizes to 0.5. Were C/D counted, A would be higher.
    """
    cands = ["A", "B", "C", "D"]
    rrf = {"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0}
    nbr = {"A": ["B"], "B": ["A", "C"], "C": ["B", "D"], "D": ["C"]}
    scores = graph_distance_scores(cands, rrf, nbr, hops=1, cohesion_decay=0.5, fanout_cap=64)
    assert scores["A"] == pytest.approx(0.5)


def test_cohesion_bounded_by_fanout_cap():
    """At most ``fanout_cap`` neighbours per node are honoured.

    A has two 1-hop neighbours B, C. With cap=1 only B counts, so A ties the
    others at 1.0; with cap=2 both count, lifting A strictly above them.
    """
    cands = ["A", "B", "C"]
    rrf = {"A": 1.0, "B": 1.0, "C": 1.0}
    nbr = {"A": ["B", "C"], "B": ["A"], "C": ["A"]}
    capped = graph_distance_scores(cands, rrf, nbr, hops=1, cohesion_decay=0.5, fanout_cap=1)
    full = graph_distance_scores(cands, rrf, nbr, hops=1, cohesion_decay=0.5, fanout_cap=2)
    assert capped["A"] == pytest.approx(capped["B"])
    assert full["A"] > full["B"]


# ===========================================================================
# T012 — Unit tests for ``resolve_anchor`` + anchor boost (US2). Written
# first; MUST FAIL until T016 (currently raises NotImplementedError).
# ===========================================================================


def _fusion(name: str, rrf: float) -> RrfFusion:
    return RrfFusion(name=name, vector_rank=1, fulltext_rank=None, rrf_score=rrf)


def test_resolve_anchor_by_name():
    """The query resolves to the candidate whose name it mentions."""
    cands = [_fusion("Neo4j", 1.0), _fusion("Cypher", 0.5)]
    anchor = resolve_anchor("how does Neo4j scale writes", cands)
    assert anchor.resolved is True
    assert anchor.name == "Neo4j"


def test_resolve_anchor_unresolved_is_not_error():
    """No candidate mentioned ⇒ unresolved (a normal, non-error outcome)."""
    cands = [_fusion("Neo4j", 1.0), _fusion("Cypher", 0.5)]
    anchor = resolve_anchor("completely unrelated query", cands)
    assert anchor.resolved is False


def test_resolve_anchor_multi_picks_highest_relevance():
    """Several names mentioned ⇒ the highest-rrf candidate wins."""
    cands = [_fusion("Neo4j", 0.4), _fusion("Cypher", 0.9)]
    anchor = resolve_anchor("Neo4j and Cypher together", cands)
    assert anchor.name == "Cypher"


def test_resolve_anchor_tie_broken_by_name():
    """Equal relevance among matches ⇒ lexicographically smallest name."""
    cands = [_fusion("beta", 0.5), _fusion("alpha", 0.5)]
    anchor = resolve_anchor("compare alpha vs beta", cands)
    assert anchor.name == "alpha"


def test_anchor_boost_lifts_nodes_closer_to_anchor():
    """With cohesion neutralised, a node nearer the anchor scores higher.

    rrf all 0 ⇒ cohesion contributes 0; score = anchor_beta·1/(1+dist):
        ANCH (d0)=0.5, near (d1)=0.25, far (d2)=0.167  (anchor_beta=0.5).
    """
    cands = ["ANCH", "near", "far"]
    rrf = {"ANCH": 0.0, "near": 0.0, "far": 0.0}
    nbr = {"ANCH": ["near"], "near": ["ANCH", "far"], "far": ["near"]}
    anchor = QueryAnchor(node_id="", label="", name="ANCH", resolved=True)
    scores = graph_distance_scores(
        cands,
        rrf,
        nbr,
        hops=2,
        cohesion_decay=0.5,
        fanout_cap=64,
        anchor=anchor,
        anchor_beta=0.5,
    )
    assert scores["ANCH"] > scores["near"] > scores["far"]


def test_unresolved_anchor_equals_cohesion_only():
    """An unresolved anchor behaves exactly like no anchor."""
    cands = ["A", "B", "C"]
    rrf = {"A": 1.0, "B": 0.5, "C": 0.3}
    nbr = {"A": ["B"], "B": ["A", "C"], "C": ["B"]}
    kw = dict(hops=2, cohesion_decay=0.5, fanout_cap=64)
    none_anchor = graph_distance_scores(cands, rrf, nbr, **kw)
    unresolved = graph_distance_scores(cands, rrf, nbr, anchor=QueryAnchor.unresolved(), **kw)
    assert none_anchor == unresolved


# ===========================================================================
# T013 — Tenancy tests (Constitution P9). Written first; MUST FAIL until
# T015/T016. The pure layer only ever sees the scope-filtered candidate
# window, so an out-of-tenant node cannot influence the ranking.
# ===========================================================================


def test_cross_tenant_neighbour_excluded():
    """A neighbour outside the candidate window contributes nothing.

    The scoped store omits cross-tenant nodes from the neighbour list, but
    even if one slipped in by name it must not count: cohesion sums only
    over the in-window candidates. A's score with a stray ``foreign``
    neighbour equals its score without it.
    """
    cands = ["A", "B"]
    rrf = {"A": 1.0, "B": 1.0}
    kw = dict(hops=1, cohesion_decay=0.5, fanout_cap=64)
    with_foreign = graph_distance_scores(cands, rrf, {"A": ["B", "foreign"], "B": ["A"]}, **kw)
    without = graph_distance_scores(cands, rrf, {"A": ["B"], "B": ["A"]}, **kw)
    assert with_foreign == without
    assert "foreign" not in with_foreign


def test_anchor_resolution_scoped():
    """An out-of-tenant name never resolves as anchor.

    Resolution only considers the (already scope-filtered) candidates, so a
    query naming a node absent from the window yields ``unresolved``.
    """
    cands = [_fusion("InTenant", 1.0)]
    anchor = resolve_anchor("give me the ForeignSecret data", cands)
    assert anchor.resolved is False
