"""
Engrama — Hybrid search engine (DDR-003 Phase C).

Combines fulltext search (graph) with vector similarity search to
produce a single ranked result list.  Talks only to protocols — zero
database-specific code.

Graceful degradation:

* ``EMBEDDING_PROVIDER=none`` → α forced to 0.0, fulltext only.
* ``VECTOR_BACKEND=none`` → same as above.
* Ollama not running → fallback to fulltext + warning.
* Node has no embedding → appears in fulltext results only.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from engrama.core.rerank import (
    RrfFusion,
    graph_distance_scores,
    resolve_anchor,
    rrf_fuse,
)

logger = logging.getLogger("engrama.core.search")

# Default trust score for a node that has no ``trust_level`` property.
# 0.5 is the same neutral middle that ``default_trust_for`` returns for
# unknown sources, so legacy nodes written before DDR-003 Phase E rank
# halfway between high-trust (sync/cli) and low-trust hypotheticals.
DEFAULT_TRUST_SCORE: float = 0.5

# Default temporal score when ``updated_at`` is missing from a result.
# Using 1.0 (the dataclass default for "fresh as today") would let a
# vector-only hit with no freshness info pretend to be brand new and
# steal an unfair recency boost from a legitimate hit. 0.5 is the same
# neutral middle used everywhere else in this module for "unknown".
DEFAULT_TEMPORAL_SCORE: float = 0.5


# ---------------------------------------------------------------------------
# Env parsing helpers (spec 002 — fail-fast config loader)
# ---------------------------------------------------------------------------
#
# Contract §3 (search-ranking.md): every spec-002 ranking knob is overridable
# by an ``ENGRAMA_*`` env var, and an unknown/invalid value **fails fast** at
# config construction with a clear error — never a silent fallback to a
# surprising ranking mode. (The pre-existing ``ENGRAMA_TRUST_DELTA`` keeps its
# older warn-and-ignore semantics; see ``HybridConfig.__post_init__``.)
#
# Each helper returns ``None`` when the var is unset (caller keeps the
# dataclass default) and raises ``ValueError`` on a malformed/out-of-range
# value.

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_choice(name: str, choices: tuple[str, ...]) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val not in choices:
        raise ValueError(f"Invalid {name}={raw!r}: expected one of {', '.join(choices)}")
    return val


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    raise ValueError(
        f"Invalid {name}={raw!r}: expected a boolean (1/0, true/false, yes/no, on/off)"
    )


def _env_int(name: str, *, minimum: int) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        val = int(raw)
    except ValueError:
        raise ValueError(f"Invalid {name}={raw!r}: expected an integer") from None
    if val < minimum:
        raise ValueError(f"Invalid {name}={raw!r}: must be >= {minimum}")
    return val


def _env_float(
    name: str,
    *,
    minimum: float,
    maximum: float | None = None,
    min_exclusive: bool = False,
) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        val = float(raw)
    except ValueError:
        raise ValueError(f"Invalid {name}={raw!r}: expected a float") from None
    low_ok = val > minimum if min_exclusive else val >= minimum
    if not low_ok or (maximum is not None and val > maximum):
        bound = (
            f"in ({minimum}, {maximum}]"
            if min_exclusive and maximum is not None
            else f">= {minimum}"
            if maximum is None
            else f"in [{minimum}, {maximum}]"
        )
        raise ValueError(f"Invalid {name}={raw!r}: must be {bound}")
    return val


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class HybridConfig:
    """Tuning knobs for hybrid search.

    Spec 002 (hybrid reranking) makes Reciprocal Rank Fusion the default
    relevance base and adds a typed-graph node-distance signal. The legacy
    linear blend is preserved for one-flag revert (``fusion_mode="linear"``);
    see ``specs/002-hybrid-reranking/data-model.md`` for the full contract.

    Attributes:
        alpha: Legacy-mode vector/fulltext blend weight (0.0 = fulltext
            only, 1.0 = vector only). Used **only** when
            ``fusion_mode="linear"``. Default ``0.6`` per DDR-003.
        fusion_mode: Relevance base — ``"rrf"`` (new default, scale-invariant
            Reciprocal Rank Fusion) or ``"linear"`` (legacy min-max blend).
        rrf_k: RRF constant ``k``; larger flattens the top-rank advantage.
            Default ``60`` (canonical). Used only in ``rrf`` mode.
        graph_beta: Weight for the graph signal — node-distance in ``rrf``
            mode, capped degree ``graph_boost`` in ``linear`` mode.
        graph_rerank: Toggle the node-distance stage (``rrf`` mode only).
            ``False`` ⇒ graph term contributes 0.
        graph_distance_hops: Max hops for cohesion + anchor distance.
            Default ``2``.
        cohesion_decay: Per-hop proximity decay in ``(0, 1]``. Default
            ``0.5`` (each extra hop halves the contribution).
        anchor_boost: Toggle the query-anchor sub-mode within the
            node-distance stage. ``False`` ⇒ cohesion only.
        anchor_beta: Weight of the anchor-distance term within
            ``graph_distance_score``. Default ``0.5``.
        fanout_cap: Max neighbours fetched per candidate — bounds graph
            rerank latency (RG-7). Default ``64``.
        boost_cap: Maximum degree ``graph_boost`` per node. Used **only**
            in ``linear`` mode.
        vector_k: Candidate count from vector search.
        fulltext_k: Candidate count from fulltext search.
        temporal_gamma: Weight for the temporal signal (Phase D).
            ``0.0`` disables temporal scoring.
        recency_half_life: Days after which recency factor is 0.5.
        trust_delta: Weight for the per-node trust signal (DDR-003 Phase
            E layer 3).  ``0.0`` disables; default ``0.1`` matches the
            DDR. Read from ``ENGRAMA_TRUST_DELTA`` at instantiation time
            so operators can tune without touching code.
    """

    alpha: float = 0.6
    # Spec 002 — relevance base + node-distance rerank knobs.
    fusion_mode: str = "rrf"
    rrf_k: int = 60
    graph_beta: float = 0.15
    graph_rerank: bool = True
    graph_distance_hops: int = 2
    cohesion_decay: float = 0.5
    anchor_boost: bool = True
    anchor_beta: float = 0.5
    fanout_cap: int = 64
    # Legacy linear-mode knobs (used only when fusion_mode="linear").
    boost_cap: float = 0.3
    vector_k: int = 20
    fulltext_k: int = 20
    temporal_gamma: float = 0.1
    recency_half_life: float = 30.0
    trust_delta: float = 0.1

    def __post_init__(self) -> None:
        # Pre-existing knob: warn-and-ignore on invalid (older semantics,
        # locked by tests/test_trust_aware_search.py). Spec 002 knobs below
        # use fail-fast instead.
        raw = os.environ.get("ENGRAMA_TRUST_DELTA")
        if raw is not None:
            try:
                self.trust_delta = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid ENGRAMA_TRUST_DELTA=%r (expected float)",
                    raw,
                )

        # Spec 002 ranking knobs — fail-fast (contract §3). A malformed value
        # raises here at construction rather than silently picking a default,
        # so an operator never gets a surprising ranking mode.
        fusion_mode = _env_choice("ENGRAMA_FUSION_MODE", ("rrf", "linear"))
        if fusion_mode is not None:
            self.fusion_mode = fusion_mode

        rrf_k = _env_int("ENGRAMA_RRF_K", minimum=1)
        if rrf_k is not None:
            self.rrf_k = rrf_k

        graph_rerank = _env_bool("ENGRAMA_GRAPH_RERANK")
        if graph_rerank is not None:
            self.graph_rerank = graph_rerank

        hops = _env_int("ENGRAMA_GRAPH_HOPS", minimum=1)
        if hops is not None:
            self.graph_distance_hops = hops

        cohesion_decay = _env_float(
            "ENGRAMA_COHESION_DECAY", minimum=0.0, maximum=1.0, min_exclusive=True
        )
        if cohesion_decay is not None:
            self.cohesion_decay = cohesion_decay

        anchor_boost = _env_bool("ENGRAMA_ANCHOR_BOOST")
        if anchor_boost is not None:
            self.anchor_boost = anchor_boost

        anchor_beta = _env_float("ENGRAMA_ANCHOR_BETA", minimum=0.0)
        if anchor_beta is not None:
            self.anchor_beta = anchor_beta

        fanout_cap = _env_int("ENGRAMA_FANOUT_CAP", minimum=1)
        if fanout_cap is not None:
            self.fanout_cap = fanout_cap

        # Composite one-flag revert (data-model.md, RG-6). Applied LAST so it
        # authoritatively overrides any individual fusion_mode/graph_rerank
        # env override: ENGRAMA_RANKING_LEGACY=1 ⇒ legacy linear blend with
        # the degree graph_boost (inherent to fusion_mode="linear"). The
        # byte-for-byte legacy-branch guarantee is finished in US3 (T023/T024).
        ranking_legacy = _env_bool("ENGRAMA_RANKING_LEGACY")
        if ranking_legacy:
            self.fusion_mode = "linear"
            self.graph_rerank = False


# ---------------------------------------------------------------------------
# Mode descriptor
# ---------------------------------------------------------------------------


@dataclass
class SearchMode:
    """Describes how a hybrid search call actually ran.

    Populated on the engine as ``last_mode`` after every ``search`` /
    ``asearch`` call so callers can detect silent degradation
    (e.g. the embeddings provider being unreachable) and surface it to
    the end user instead of mistaking a fulltext-only result list for
    a full hybrid hit.

    Attributes:
        mode: One of ``"hybrid"`` (both paths ran), ``"fulltext_only"``
            (vector path was skipped — by config or because it failed),
            or ``"vector_only"`` (fulltext path returned empty / errored
            — rare).
        degraded: ``True`` iff a path was *attempted* but failed at
            runtime. ``mode="fulltext_only"`` with ``degraded=False``
            means vector search is disabled by configuration (no
            embedder, ``EMBEDDING_PROVIDER=none``, etc.).
        reason: Short human-readable explanation when ``degraded`` is
            ``True``. Empty otherwise.
    """

    mode: str = "fulltext_only"
    degraded: bool = False
    reason: str = ""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """A single hybrid search result."""

    node_id: str = ""
    """Neo4j elementId (for vector store cross-referencing)."""

    label: str = ""
    """Primary node label (e.g. ``"Project"``)."""

    name: str = ""
    """Node identity (name or title)."""

    vector_score: float = 0.0
    """Normalised vector similarity score (0-1)."""

    fulltext_score: float = 0.0
    """Normalised fulltext score (0-1)."""

    vector_rank: int | None = None
    """1-based rank in the vector channel (spec 002). ``None`` if the result
    did not appear in the vector channel. At least one of ``vector_rank`` /
    ``fulltext_rank`` is non-``None`` for every result."""

    fulltext_rank: int | None = None
    """1-based rank in the fulltext channel (spec 002). ``None`` if absent."""

    rrf_score: float = 0.0
    """Normalised Reciprocal Rank Fusion relevance base, 0-1 (spec 002).

    The fused signal that replaces the min-max linear blend in
    ``fusion_mode="rrf"``. Unused (left 0) in ``linear`` mode."""

    graph_boost: float = 0.0
    """Degree-count graph boost (e.g. relationship count). Legacy signal —
    populated only in ``fusion_mode="linear"``; replaced by
    ``graph_distance_score`` in ``rrf`` mode."""

    graph_distance_score: float = 0.0
    """Node-distance graph signal, 0-1 (spec 002): result-set cohesion plus
    an optional query-anchor distance boost. Replaces ``graph_boost`` in
    ``fusion_mode="rrf"``; 0 in ``linear`` mode."""

    temporal_score: float = 1.0
    """Temporal relevance (confidence × recency), 0-1."""

    trust_score: float = DEFAULT_TRUST_SCORE
    """Per-node provenance trust (DDR-003 Phase E), 0-1.

    Defaults to :data:`DEFAULT_TRUST_SCORE` (0.5) when the node has no
    ``trust_level`` property — that is, for nodes written before
    provenance landed.
    """

    final_score: float = 0.0
    """Weighted combination of all signals."""

    properties: dict[str, Any] = field(default_factory=dict)
    """Selected node properties returned with the result."""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class HybridSearchEngine:
    """Fuses fulltext and vector search results.

    Parameters:
        graph_store: Any ``GraphStore`` implementation.
        vector_store: Any ``VectorStore`` implementation.
        embedder: Any ``EmbeddingProvider`` implementation.
        config: Tuning knobs.  Defaults are from DDR-003.
    """

    def __init__(
        self,
        graph_store: Any,
        vector_store: Any,
        embedder: Any,
        config: HybridConfig | None = None,
        *,
        scope: Any = None,
    ) -> None:
        self.graph = graph_store
        self.vector = vector_store
        self.embedder = embedder
        self.config = config or HybridConfig()
        # DDR-003 Phase F: scope is forwarded verbatim to fulltext_search
        # and search_vectors on every (a)search call.
        self.scope = scope

        # Auto-detect when vector search is unavailable
        self._vector_enabled: bool = (
            getattr(embedder, "dimensions", 0) > 0 and getattr(vector_store, "dimensions", 0) > 0
        )

        # Populated by every (a)search() call so callers can introspect
        # whether the result list came from a full hybrid run or a
        # degraded fallback path.
        self.last_mode: SearchMode = SearchMode(
            mode="fulltext_only" if not self._vector_enabled else "hybrid"
        )

        # The query embedding from the most recent (a)search, or None when the
        # vector channel didn't run. Lets callers reuse it (e.g. the MCP
        # proactive-insight gate) instead of re-embedding the same query.
        self.last_query_vector: list[float] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Run a hybrid search and return ranked results.

        Args:
            query: Natural-language search string.
            limit: Maximum results to return.

        Returns:
            List of :class:`SearchResult` ordered by ``final_score``.
            Inspect ``self.last_mode`` after the call for whether the
            vector path actually ran or fell back to fulltext-only.
        """
        alpha = self.config.alpha
        vector_reason = ""

        # --- Vector branch ---
        v_results: list[dict[str, Any]] = []
        if self._vector_enabled:
            try:
                query_vec = self.embedder.embed(query)
                if query_vec:
                    v_results = self.vector.search_vectors(
                        query_vec,
                        limit=self.config.vector_k,
                        scope=self.scope,
                    )
                else:
                    vector_reason = "embedder returned an empty vector"
                    alpha = 0.0
            except Exception as e:
                logger.warning("Vector search failed, falling back to fulltext: %s", e)
                alpha = 0.0
                vector_reason = f"vector path failed: {type(e).__name__}: {e}"
        else:
            alpha = 0.0

        # --- Fulltext branch ---
        f_results = self.graph.fulltext_search(
            query, limit=self.config.fulltext_k, scope=self.scope
        )

        self.last_mode = self._compute_mode(vector_reason)

        # --- Merge ---
        merged = self._merge(v_results, f_results, alpha)

        # --- Graph-aware node-distance rerank (spec 002 US2, sync path) ---
        if self.config.fusion_mode == "rrf" and self.config.graph_rerank:
            self._graph_rerank_sync(merged, query, self.scope)

        # --- Rank ---
        merged.sort(key=lambda r: r.final_score, reverse=True)
        return merged[:limit]

    async def asearch(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Async hybrid search — for MCP server and async callers.

        Same algorithm as :meth:`search` but uses ``aembed`` and async
        store methods. Inspect ``self.last_mode`` after the call for
        the actual execution mode.
        """
        alpha = self.config.alpha
        vector_reason = ""
        # Reset; populated below so callers (e.g. the MCP proactive-insight
        # gate) can reuse the query embedding instead of re-embedding it.
        self.last_query_vector = None

        # --- Vector branch ---
        v_results: list[dict[str, Any]] = []
        if self._vector_enabled:
            try:
                query_vec = await self.embedder.aembed(query)
                if query_vec:
                    self.last_query_vector = query_vec
                    v_results = await self.vector.search_similar(
                        query_vec,
                        limit=self.config.vector_k,
                        scope=self.scope,
                    )
                else:
                    vector_reason = "embedder returned an empty vector"
                    alpha = 0.0
            except Exception as e:
                logger.warning("Async vector search failed, falling back: %s", e)
                alpha = 0.0
                vector_reason = f"vector path failed: {type(e).__name__}: {e}"
        else:
            alpha = 0.0

        # --- Fulltext branch ---
        f_results = await self.graph.fulltext_search(
            query, limit=self.config.fulltext_k, scope=self.scope
        )

        self.last_mode = self._compute_mode(vector_reason)

        # --- Merge ---
        merged = self._merge(v_results, f_results, alpha)

        # --- Graph-aware node-distance rerank (spec 002 US2) ---
        # Runs over the full fused candidate window before the final cut, so
        # cohesion/anchor see every co-retrieved node (RG-7 bounds the cost).
        if self.config.fusion_mode == "rrf" and self.config.graph_rerank:
            await self._graph_rerank(merged, query, self.scope)

        # --- Rank ---
        merged.sort(key=lambda r: r.final_score, reverse=True)
        return merged[:limit]

    async def _graph_rerank(
        self,
        results: list[SearchResult],
        query: str,
        scope: Any,
    ) -> None:
        """Async node-distance rerank: fetch neighbours, then score.

        For each candidate it fetches the in-tenant 1-hop neighbours via the
        scoped async ``get_node_with_neighbours`` and keeps only those inside
        the candidate window; the shared scorer then folds cohesion (and the
        optional anchor boost) into ``final_score``.
        """
        named = self._graph_candidates(results)
        if named is None:
            return
        candidate_set = {r.name for r in named}
        neighbours: dict[str, list[str]] = {}
        for r in named:
            try:
                data = await self.graph.get_node_with_neighbours(
                    r.label, "name", r.name, hops=1, scope=scope
                )
            except Exception as e:
                logger.warning("Graph rerank neighbour fetch failed for %r: %s", r.name, e)
                data = None
            neighbours[r.name] = self._window_neighbours(data, candidate_set)
        self._apply_graph_scores(named, query, neighbours)

    def _graph_rerank_sync(
        self,
        results: list[SearchResult],
        query: str,
        scope: Any,
    ) -> None:
        """Sync counterpart of :meth:`_graph_rerank` (spec 002 T019).

        Uses the sync store's ``get_node_with_neighbours``; the scoring math
        is identical. No event loop is touched — the async path stays the
        first-class one, this keeps a pure-sync ``search()`` working too.
        """
        named = self._graph_candidates(results)
        if named is None:
            return
        candidate_set = {r.name for r in named}
        neighbours: dict[str, list[str]] = {}
        for r in named:
            try:
                data = self.graph.get_node_with_neighbours(
                    r.label, "name", r.name, hops=1, scope=scope
                )
            except Exception as e:
                logger.warning("Graph rerank neighbour fetch failed for %r: %s", r.name, e)
                data = None
            neighbours[r.name] = self._window_neighbours(data, candidate_set)
        self._apply_graph_scores(named, query, neighbours)

    def _graph_candidates(self, results: list[SearchResult]) -> list[SearchResult] | None:
        """Return the named candidates to rerank, or ``None`` to skip cleanly.

        Skips when there is nothing to rerank or the store cannot supply
        neighbours (graph rerank is then a no-op, scores stay 0).
        """
        if not results or not hasattr(self.graph, "get_node_with_neighbours"):
            return None
        named = [r for r in results if r.name]
        return named or None

    @staticmethod
    def _window_neighbours(data: Any, candidate_set: set[str]) -> list[str]:
        """Names of a node's neighbours that fall inside the candidate window.

        Restricting to the window is also where any stray out-of-tenant
        neighbour is dropped (defence-in-depth on top of scoped fetch; RG-4).
        """
        if not data:
            return []
        out: list[str] = []
        for n in data.get("neighbours", []):
            nm = n.get("name")
            if nm and nm in candidate_set:
                out.append(nm)
        return out

    def _apply_graph_scores(
        self,
        named: list[SearchResult],
        query: str,
        neighbours: dict[str, list[str]],
    ) -> None:
        """Compute ``graph_distance_score`` over the induced subgraph and fold
        it into ``final_score`` as ``graph_beta · graph_distance_score`` (T018).

        Replaces the legacy degree ``graph_boost`` in rrf mode; honours the
        ``anchor_boost`` toggle. Backend-agnostic — both (a)search paths share
        it so SQLite and Neo4j rank identically (FR-012).
        """
        candidate_names = [r.name for r in named]
        rrf_scores = {r.name: r.rrf_score for r in named}

        anchor = None
        if self.config.anchor_boost:
            fused = [
                RrfFusion(
                    name=r.name,
                    vector_rank=r.vector_rank,
                    fulltext_rank=r.fulltext_rank,
                    rrf_score=r.rrf_score,
                )
                for r in named
            ]
            anchor = resolve_anchor(query, fused)

        scores = graph_distance_scores(
            candidate_names,
            rrf_scores,
            neighbours,
            hops=self.config.graph_distance_hops,
            cohesion_decay=self.config.cohesion_decay,
            fanout_cap=self.config.fanout_cap,
            anchor=anchor,
            anchor_beta=self.config.anchor_beta,
        )

        beta = self.config.graph_beta
        for r in named:
            r.graph_distance_score = scores.get(r.name, 0.0)
            r.final_score += beta * r.graph_distance_score

    def _compute_mode(self, vector_reason: str) -> SearchMode:
        """Build the :class:`SearchMode` descriptor for the most recent
        search call.

        ``vector_reason`` is non-empty iff the vector path was attempted
        but did not produce results (failure or empty embedding).
        """
        if not self._vector_enabled:
            return SearchMode(mode="fulltext_only", degraded=False)
        if vector_reason:
            return SearchMode(
                mode="fulltext_only",
                degraded=True,
                reason=vector_reason,
            )
        return SearchMode(mode="hybrid", degraded=False)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _merge(
        self,
        v_results: list[dict[str, Any]],
        f_results: list[Any],
        alpha: float,
    ) -> list[SearchResult]:
        """Merge vector and fulltext results by node identity.

        Normalises each score list to [0, 1] using min-max scaling,
        then computes the final weighted score.
        """
        # Build lookup by name (the common identity across both result sets)
        by_name: dict[str, SearchResult] = {}

        # --- Normalise and index vector results ---
        if v_results:
            v_scores = [r.get("score", 0.0) for r in v_results]
            v_min, v_max = min(v_scores), max(v_scores)
            v_range = v_max - v_min
            # When all scores are identical (inc. single result), normalise to 1.0
            v_all_equal = v_range == 0

            for r in v_results:
                name = r.get("name", "")
                if not name:
                    continue
                norm = 1.0 if v_all_equal else (r.get("score", 0.0) - v_min) / v_range
                # Copy enrichment fields when the vector backend exposes
                # them. Pure-semantic hits (no fulltext overlap) would
                # otherwise carry an empty ``properties`` dict, which
                # surfaces as ``summary=""`` / ``tags=[]`` in the MCP
                # response and starves the caller of context.
                vec_props: dict[str, Any] = {}
                for k in ("confidence", "updated_at", "summary", "tags", "trust_level"):
                    if k in r and r[k] is not None:
                        vec_props[k] = r[k]
                sr = SearchResult(
                    node_id=r.get("node_id", ""),
                    label=r.get("label", ""),
                    name=name,
                    vector_score=norm,
                    properties=vec_props,
                )
                by_name[name] = sr

        # --- Normalise and index fulltext results ---
        if f_results:
            # fulltext results may be Record objects or dicts
            f_dicts = [dict(r) if not isinstance(r, dict) else r for r in f_results]
            f_scores = [d.get("score", 0.0) for d in f_dicts]
            f_min, f_max = min(f_scores), max(f_scores)
            f_range = f_max - f_min
            f_all_equal = f_range == 0

            for d in f_dicts:
                name = d.get("name", "")
                if not name:
                    continue
                norm = 1.0 if f_all_equal else (d.get("score", 0.0) - f_min) / f_range
                # Build properties dict: temporal signals (Phase D) plus the
                # enrichment fields (summary, tags) exposed via fulltext search.
                # ``details`` is intentionally omitted — use engrama_context
                # when full context is needed.
                props: dict[str, Any] = {}
                for k in ("confidence", "updated_at", "summary", "tags", "trust_level"):
                    if k in d:
                        props[k] = d[k]

                if name in by_name:
                    by_name[name].fulltext_score = norm
                    if not by_name[name].label:
                        by_name[name].label = d.get("type", "")
                    # Merge props (temporal + enrichment)
                    by_name[name].properties.update(props)
                else:
                    by_name[name] = SearchResult(
                        label=d.get("type", ""),
                        name=name,
                        fulltext_score=norm,
                        properties=props,
                    )

        # --- RRF fusion (spec 002 US1) ---
        # In ``rrf`` mode the rank-based fusion replaces min-max magnitudes as
        # the relevance base. Ranks are derived from each channel's score
        # order (descending); a down/empty vector channel yields an empty
        # vector list, so rrf_fuse degrades to the fulltext channel's order —
        # the single-channel fallback (RG-2/RG-5). The ``degraded``/``mode``
        # signal is computed independently in ``_compute_mode`` and unchanged.
        if self.config.fusion_mode == "rrf":
            v_ranked = sorted(
                (r for r in v_results if r.get("name")),
                key=lambda r: r.get("score", 0.0),
                reverse=True,
            )
            f_ranked = sorted(
                (dict(r) if not isinstance(r, dict) else r for r in f_results),
                key=lambda d: d.get("score", 0.0),
                reverse=True,
            )
            fused = rrf_fuse(
                [r["name"] for r in v_ranked],
                [d["name"] for d in f_ranked if d.get("name")],
                k=self.config.rrf_k,
            )
            for name, fusion in fused.items():
                sr = by_name.get(name)
                if sr is not None:
                    sr.rrf_score = fusion.rrf_score
                    sr.vector_rank = fusion.vector_rank
                    sr.fulltext_rank = fusion.fulltext_rank

        # --- Temporal scoring (Phase D) ---
        gamma = self.config.temporal_gamma
        if gamma > 0:
            from engrama.core.temporal import days_since, temporal_score

            for sr in by_name.values():
                updated_at = sr.properties.get("updated_at")
                if updated_at:
                    confidence = sr.properties.get("confidence", 1.0)
                    days = days_since(updated_at)
                    sr.temporal_score = temporal_score(
                        confidence if confidence is not None else 1.0,
                        days,
                        recency_half_life=self.config.recency_half_life,
                    )
                else:
                    # No freshness info means "unknown", not "fresh as
                    # today" — fall back to a neutral score so a
                    # vector-only hit without ``updated_at`` doesn't
                    # outrank a real hit by stealing the recency boost.
                    sr.temporal_score = DEFAULT_TEMPORAL_SCORE

        # --- Trust scoring (Phase E layer 3) ---
        delta = self.config.trust_delta
        if delta > 0:
            for sr in by_name.values():
                trust = sr.properties.get("trust_level")
                sr.trust_score = float(trust) if trust is not None else DEFAULT_TRUST_SCORE

        # --- Score ---
        beta = self.config.graph_beta
        for sr in by_name.values():
            if self.config.fusion_mode == "rrf":
                # Spec 002 US1: RRF relevance base + temporal + trust. The
                # graph term is 0 here; US2 adds ``beta·graph_distance_score``
                # in the rerank stage. ``alpha``/``graph_boost`` are unused in
                # this mode (degradation is handled by rrf_fuse's fallback).
                sr.final_score = sr.rrf_score + gamma * sr.temporal_score + delta * sr.trust_score
            else:
                # Legacy linear blend (fusion_mode="linear"), unchanged.
                sr.final_score = (
                    alpha * sr.vector_score
                    + (1 - alpha) * sr.fulltext_score
                    + beta * min(sr.graph_boost, self.config.boost_cap)
                    + gamma * sr.temporal_score
                    + delta * sr.trust_score
                )

        return list(by_name.values())

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def vector_enabled(self) -> bool:
        """Whether vector search is active for this engine instance."""
        return self._vector_enabled

    def __repr__(self) -> str:
        return f"HybridSearchEngine(vector={self._vector_enabled}, alpha={self.config.alpha})"
