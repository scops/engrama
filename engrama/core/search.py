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
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("engrama.core.search")


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
    """

    alpha: float = 0.6
    graph_beta: float = 0.15
    boost_cap: float = 0.3
    vector_k: int = 20
    fulltext_k: int = 20
    temporal_gamma: float = 0.1
    recency_half_life: float = 30.0


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
    """Normalised vector similarity score (0–1)."""

    fulltext_score: float = 0.0
    """Normalised fulltext score (0–1)."""

    graph_boost: float = 0.0
    """Graph-based boost (e.g. relationship count)."""

    temporal_score: float = 1.0
    """Temporal relevance (confidence × recency), 0–1."""

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
            getattr(embedder, "dimensions", 0) > 0
            and getattr(vector_store, "dimensions", 0) > 0
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
        """
        alpha = self.config.alpha

        # --- Vector branch ---
        v_results: list[dict[str, Any]] = []
        if self._vector_enabled:
            try:
                query_vec = self.embedder.embed(query)
                if query_vec:
                    v_results = self.vector.search_vectors(
                        query_vec, limit=self.config.vector_k,
                    )
            except (ConnectionError, RuntimeError) as e:
                logger.warning("Vector search failed, falling back to fulltext: %s", e)
                alpha = 0.0
        else:
            alpha = 0.0

        # --- Fulltext branch ---
        f_results = self.graph.fulltext_search(query, limit=self.config.fulltext_k)

        # --- Merge ---
        merged = self._merge(v_results, f_results, alpha)

        # --- Rank ---
        merged.sort(key=lambda r: r.final_score, reverse=True)
        return merged[:limit]

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
            v_range = v_max - v_min if v_max > v_min else 1.0

            for r in v_results:
                name = r.get("name", "")
                if not name:
                    continue
                norm = (r.get("score", 0.0) - v_min) / v_range
                sr = SearchResult(
                    node_id=r.get("node_id", ""),
                    label=r.get("label", ""),
                    name=name,
                    vector_score=norm,
                )
                by_name[name] = sr

        # --- Normalise and index fulltext results ---
        if f_results:
            # fulltext results may be Record objects or dicts
            f_dicts = [dict(r) if not isinstance(r, dict) else r for r in f_results]
            f_scores = [d.get("score", 0.0) for d in f_dicts]
            f_min, f_max = min(f_scores), max(f_scores)
            f_range = f_max - f_min if f_max > f_min else 1.0

            for d in f_dicts:
                name = d.get("name", "")
                if not name:
                    continue
                norm = (d.get("score", 0.0) - f_min) / f_range
                # Build temporal properties for Phase D scoring
                temporal_props = {}
                if "confidence" in d:
                    temporal_props["confidence"] = d["confidence"]
                if "updated_at" in d:
                    temporal_props["updated_at"] = d["updated_at"]

                if name in by_name:
                    by_name[name].fulltext_score = norm
                    if not by_name[name].label:
                        by_name[name].label = d.get("type", "")
                    # Merge temporal props
                    by_name[name].properties.update(temporal_props)
                else:
                    by_name[name] = SearchResult(
                        label=d.get("type", ""),
                        name=name,
                        fulltext_score=norm,
                        properties=temporal_props,
                    )

        # --- Temporal scoring (Phase D) ---
        gamma = self.config.temporal_gamma
        if gamma > 0:
            from engrama.core.temporal import temporal_score, days_since

            for sr in by_name.values():
                confidence = sr.properties.get("confidence", 1.0)
                updated_at = sr.properties.get("updated_at")
                days = days_since(updated_at) if updated_at else 0.0
                sr.temporal_score = temporal_score(
                    confidence if confidence is not None else 1.0,
                    days,
                    recency_half_life=self.config.recency_half_life,
                )

        # --- Score ---
        beta = self.config.graph_beta
        for sr in by_name.values():
            sr.final_score = (
                alpha * sr.vector_score
                + (1 - alpha) * sr.fulltext_score
                + beta * min(sr.graph_boost, self.config.boost_cap)
                + gamma * sr.temporal_score
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
        return (
            f"HybridSearchEngine(vector={self._vector_enabled}, "
            f"alpha={self.config.alpha})"
        )
