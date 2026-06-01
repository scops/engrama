"""
Engrama — Pure hybrid reranking primitives (spec 002-hybrid-reranking).

Backend-agnostic ranking math used by :class:`engrama.core.search.HybridSearchEngine`:

* :func:`rrf_fuse` — Reciprocal Rank Fusion of the vector and fulltext
  channels into a single, scale-invariant relevance base (replaces the
  min-max linear blend; spec US1).
* :func:`graph_distance_scores` — typed-graph node-distance signal:
  result-set *cohesion* always, plus an optional *query-anchor* distance
  boost (replaces the degree-count ``graph_boost``; spec US2).
* :func:`resolve_anchor` — pick the in-tenant node the query refers to,
  among the already-scope-filtered fused candidates (spec US2).

Design contract (``specs/002-hybrid-reranking/contracts/search-ranking.md``):

* **Pure** — no I/O, no backend imports, no env reads. Scope isolation is
  inherited: callers pass only candidates/neighbours that the scoped store
  already returned, so an out-of-tenant node can never enter the math
  (RG-4, and the data-model tenancy table).
* **Deterministic** — identical inputs ⇒ identical output; ties are broken
  by rank, then lexicographically by name (RG-3).
* **Normalized & NaN-safe** — ``rrf_score`` and ``graph_distance_score``
  land in ``[0, 1]`` and are guarded for single-result / all-equal windows.

Every function here is a typed stub pending its failing test (P7, TDD):
``rrf_fuse`` → T008, ``graph_distance_scores`` → T015, ``resolve_anchor`` →
T016.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Reciprocal Rank Fusion default constant. 60 is the canonical value from
# Cormack et al. (2009); large enough that top-rank dominance is gentle.
DEFAULT_RRF_K: int = 60


# ---------------------------------------------------------------------------
# Transient ranking entities (never persisted; see data-model.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RrfFusion:
    """Per-candidate output of :func:`rrf_fuse` (transient to one query).

    Attributes:
        name: Node identity (the common key across both channels).
        vector_rank: 1-based rank in the vector channel, ``None`` if the
            candidate did not appear there.
        fulltext_rank: 1-based rank in the fulltext channel, ``None`` if
            absent. At least one of the two ranks is non-``None``.
        rrf_score: Fused relevance base, normalized to ``[0, 1]``.
    """

    name: str
    vector_rank: int | None
    fulltext_rank: int | None
    rrf_score: float


@dataclass(frozen=True)
class QueryAnchor:
    """The in-tenant node a query refers to (transient; data-model.md).

    ``resolved=False`` is a normal, non-error outcome (FR-005): the query
    matched no candidate, so graph reranking proceeds with cohesion only.
    """

    node_id: str
    label: str
    name: str
    resolved: bool

    @classmethod
    def unresolved(cls) -> QueryAnchor:
        """An anchor that did not resolve — cohesion-only downstream."""
        return cls(node_id="", label="", name="", resolved=False)


# ---------------------------------------------------------------------------
# US1 — Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def rrf_fuse(
    vector_list: Sequence[str],
    fulltext_list: Sequence[str],
    k: int = DEFAULT_RRF_K,
) -> dict[str, RrfFusion]:
    """Fuse two ranked channels via Reciprocal Rank Fusion.

    Each input is an ordered list of node names, best-first (rank 1 =
    index 0). A candidate's raw RRF weight is ``Σ 1/(k + rank)`` over the
    channels it appears in; the result is min-max normalized to ``[0, 1]``.

    Guarantees (contract RG-1, RG-2, RG-3):

    * **Scale invariant** — only ranks are used, never raw scores, so
      rescaling either channel cannot change the order.
    * **Single-channel fallback** — if one list is empty, the output is the
      other channel's order; no error, no empty-on-degrade.
    * **Deterministic** — ties broken by best available rank, then by name.

    Args:
        vector_list: Vector-channel node names, best-first.
        fulltext_list: Fulltext-channel node names, best-first.
        k: RRF constant; larger ``k`` flattens the top-rank advantage.

    Returns:
        Mapping of node name → :class:`RrfFusion`, one entry per distinct
        name across both channels.
    """
    # 1-based rank per channel; first occurrence wins if a name repeats.
    vector_ranks: dict[str, int] = {}
    for i, name in enumerate(vector_list):
        if name and name not in vector_ranks:
            vector_ranks[name] = i + 1
    fulltext_ranks: dict[str, int] = {}
    for i, name in enumerate(fulltext_list):
        if name and name not in fulltext_ranks:
            fulltext_ranks[name] = i + 1

    # Raw RRF weight = Σ 1/(k + rank) over the channels the name appears in.
    raw: dict[str, float] = {}
    for name in (*vector_ranks, *fulltext_ranks):
        if name in raw:
            continue
        weight = 0.0
        if name in vector_ranks:
            weight += 1.0 / (k + vector_ranks[name])
        if name in fulltext_ranks:
            weight += 1.0 / (k + fulltext_ranks[name])
        raw[name] = weight

    if not raw:
        return {}

    # Min-max normalize to [0, 1]; an all-equal window (incl. a single
    # candidate) collapses to 1.0 rather than dividing by zero — mirrors the
    # per-channel convention in search._merge and guarantees no NaN.
    lo, hi = min(raw.values()), max(raw.values())
    span = hi - lo

    out: dict[str, RrfFusion] = {}
    for name, weight in raw.items():
        norm = 1.0 if span == 0 else (weight - lo) / span
        out[name] = RrfFusion(
            name=name,
            vector_rank=vector_ranks.get(name),
            fulltext_rank=fulltext_ranks.get(name),
            rrf_score=norm,
        )
    return out


# ---------------------------------------------------------------------------
# US2 — Graph-aware node-distance reranking
# ---------------------------------------------------------------------------


def graph_distance_scores(
    candidates: Sequence[str],
    rrf_scores: Mapping[str, float],
    neighbours: Mapping[str, Sequence[str]],
    *,
    hops: int,
    cohesion_decay: float,
    fanout_cap: int,
    anchor: QueryAnchor | None = None,
    anchor_beta: float = 0.5,
) -> dict[str, float]:
    """Score each candidate by its typed-graph distance to the rest.

    Computed only over the fused candidate window (never the full corpus;
    RG-7). For each candidate ``d``:

    * **cohesion** — ``Σ_{c≠d} rrf_score(c) · cohesion_decay**(dist(d,c)-1)``
      where ``dist`` is the shortest hop count through ``neighbours``
      restricted to the candidate set, bounded by ``hops``;
    * **anchor** — when ``anchor`` resolved, add ``anchor_beta · 1/(1+dist(d, anchor))``.

    The combined score is clamped to ``[0, 1]``. Cross-tenant isolation is
    inherited: ``neighbours`` must already be scope-filtered, so an
    out-of-tenant node never contributes (data-model tenancy table, RG-4).

    Args:
        candidates: Names in the fused candidate window.
        rrf_scores: Name → normalized RRF relevance (cohesion weights).
        neighbours: Name → its in-tenant neighbour names (already capped
            upstream, re-bounded here by ``fanout_cap``).
        hops: Max hop distance considered (``graph_distance_hops``).
        cohesion_decay: Per-hop proximity decay in ``(0, 1]``.
        fanout_cap: Max neighbours honoured per node (latency bound).
        anchor: Resolved query anchor, or ``None``/unresolved for
            cohesion-only.
        anchor_beta: Weight of the anchor-distance term within the score.

    Returns:
        Mapping of candidate name → ``graph_distance_score`` in ``[0, 1]``.
    """
    raise NotImplementedError("T015 — implement cohesion + anchor distance")


def resolve_anchor(
    query: str,
    candidates: Sequence[RrfFusion],
) -> QueryAnchor:
    """Resolve the query to one in-tenant anchor among the fused candidates.

    The anchor is chosen from ``candidates`` (already scope-filtered, so
    resolution is tenant-safe by construction; RG-4 / ``test_anchor_resolution_scoped``)
    by matching the query against candidate names. On multiple matches the
    pick is deterministic: highest fused relevance, tie-broken by the
    lexicographically smallest name (data-model validation rules).

    Args:
        query: The raw search string.
        candidates: Fused candidates from :func:`rrf_fuse`, in any order.

    Returns:
        A resolved :class:`QueryAnchor`, or
        :meth:`QueryAnchor.unresolved` when the query matches none — a
        normal, non-error outcome that downstream treats as cohesion-only.
    """
    raise NotImplementedError("T016 — implement scoped anchor resolution")
