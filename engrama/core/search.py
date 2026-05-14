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

logger = logging.getLogger("engrama.core.search")

# Default trust score for a node that has no ``trust_level`` property.
# 0.5 is the same neutral middle that ``default_trust_for`` returns for
# unknown sources, so legacy nodes written before DDR-003 Phase E rank
# halfway between high-trust (sync/cli) and low-trust hypotheticals.
DEFAULT_TRUST_SCORE: float = 0.5


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class HybridConfig:
    """Tuning knobs for hybrid search.

    Attributes:
        alpha: Weight for vector score (0.0 = fulltext only, 1.0 = vector
            only).  Default ``0.6`` per DDR-003.
        graph_beta: Weight for the optional graph-boost signal.
        boost_cap: Maximum graph-boost per node.
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
    graph_beta: float = 0.15
    boost_cap: float = 0.3
    vector_k: int = 20
    fulltext_k: int = 20
    temporal_gamma: float = 0.1
    recency_half_life: float = 30.0
    trust_delta: float = 0.1

    def __post_init__(self) -> None:
        raw = os.environ.get("ENGRAMA_TRUST_DELTA")
        if raw is not None:
            try:
                self.trust_delta = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid ENGRAMA_TRUST_DELTA=%r (expected float)",
                    raw,
                )


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

    graph_boost: float = 0.0
    """Graph-based boost (e.g. relationship count)."""

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
    ) -> None:
        self.graph = graph_store
        self.vector = vector_store
        self.embedder = embedder
        self.config = config or HybridConfig()

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
        f_results = self.graph.fulltext_search(query, limit=self.config.fulltext_k)

        self.last_mode = self._compute_mode(vector_reason)

        # --- Merge ---
        merged = self._merge(v_results, f_results, alpha)

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

        # --- Vector branch ---
        v_results: list[dict[str, Any]] = []
        if self._vector_enabled:
            try:
                query_vec = await self.embedder.aembed(query)
                if query_vec:
                    v_results = await self.vector.search_similar(
                        query_vec,
                        limit=self.config.vector_k,
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
        f_results = await self.graph.fulltext_search(query, limit=self.config.fulltext_k)

        self.last_mode = self._compute_mode(vector_reason)

        # --- Merge + rank (sync computation) ---
        merged = self._merge(v_results, f_results, alpha)
        merged.sort(key=lambda r: r.final_score, reverse=True)
        return merged[:limit]

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

        # --- Temporal scoring (Phase D) ---
        gamma = self.config.temporal_gamma
        if gamma > 0:
            from engrama.core.temporal import days_since, temporal_score

            for sr in by_name.values():
                confidence = sr.properties.get("confidence", 1.0)
                updated_at = sr.properties.get("updated_at")
                days = days_since(updated_at) if updated_at else 0.0
                sr.temporal_score = temporal_score(
                    confidence if confidence is not None else 1.0,
                    days,
                    recency_half_life=self.config.recency_half_life,
                )

        # --- Trust scoring (Phase E layer 3) ---
        delta = self.config.trust_delta
        if delta > 0:
            for sr in by_name.values():
                trust = sr.properties.get("trust_level")
                sr.trust_score = float(trust) if trust is not None else DEFAULT_TRUST_SCORE

        # --- Score ---
        beta = self.config.graph_beta
        for sr in by_name.values():
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
