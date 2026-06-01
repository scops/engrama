"""
Tests for the hybrid search engine (DDR-003 Phase C).

Unit tests use mock stores (no external dependencies).
Integration tests hit real Neo4j + Ollama (skipped if unavailable).
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import pytest

from engrama.core.search import (
    DEFAULT_TEMPORAL_SCORE,
    HybridConfig,
    HybridSearchEngine,
    SearchMode,
    SearchResult,
)

# ---------------------------------------------------------------------------
# Mock stores for unit tests
# ---------------------------------------------------------------------------


class _MockEmbedder:
    """Deterministic fake embedder for testing score fusion."""

    dimensions = 4

    def embed(self, text: str) -> list[float]:
        return [len(text) / 100.0, 0.5, 0.5, 0.5]

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    async def aembed_batch(self, texts: list[str]) -> list[list[float]]:
        return self.embed_batch(texts)

    def health_check(self) -> bool:
        return True

    async def ahealth_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


class _NullEmbedder:
    """Embedder with zero dimensions — simulates EMBEDDING_PROVIDER=none."""

    dimensions = 0

    def embed(self, text: str) -> list[float]:
        return []

    async def aembed(self, text: str) -> list[float]:
        return []

    def health_check(self) -> bool:
        return True

    async def ahealth_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


class _SyncMockGraphStore:
    """Fake graph store for sync tests (plain return values)."""

    def __init__(self, results: list[dict[str, Any]] | None = None):
        self._results = results or []

    def fulltext_search(self, query: str, limit: int = 10, **kwargs) -> list[dict[str, Any]]:
        return self._results[:limit]

    def get_neighbours(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []


class _AsyncMockGraphStore:
    """Fake graph store for async tests (coroutine return values)."""

    def __init__(self, results: list[dict[str, Any]] | None = None):
        self._results = results or []

    async def fulltext_search(self, query: str, limit: int = 10, **kwargs) -> list[dict[str, Any]]:
        return self._results[:limit]

    async def get_neighbours(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []


class _SyncMockVectorStore:
    """Fake vector store for sync tests."""

    dimensions = 4

    def __init__(self, results: list[dict[str, Any]] | None = None):
        self._results = results or []

    def search_vectors(
        self,
        query_embedding: list[float],
        limit: int = 10,
        scope: Any = None,
    ) -> list[dict[str, Any]]:
        return self._results[:limit]


class _AsyncMockVectorStore:
    """Fake vector store for async tests."""

    dimensions = 4

    def __init__(self, results: list[dict[str, Any]] | None = None):
        self._results = results or []

    async def search_similar(
        self,
        query_embedding: list[float],
        limit: int = 10,
        scope: Any = None,
    ) -> list[dict[str, Any]]:
        return self._results[:limit]


class _SyncNullVectorStore:
    """Vector store with zero dimensions (sync)."""

    dimensions = 0

    def search_vectors(self, *a, **kw) -> list[dict[str, Any]]:
        return []


class _AsyncNullVectorStore:
    """Vector store with zero dimensions (async)."""

    dimensions = 0

    async def search_similar(self, *a, **kw) -> list[dict[str, Any]]:
        return []


# ---------------------------------------------------------------------------
# HybridConfig tests
# ---------------------------------------------------------------------------


class TestHybridConfig:
    """Test default configuration values."""

    def test_defaults(self):
        cfg = HybridConfig()
        assert cfg.alpha == 0.6
        assert cfg.graph_beta == 0.15
        assert cfg.boost_cap == 0.3
        assert cfg.vector_k == 20
        assert cfg.fulltext_k == 20

    def test_custom_values(self):
        cfg = HybridConfig(alpha=0.8, graph_beta=0.2)
        assert cfg.alpha == 0.8
        assert cfg.graph_beta == 0.2


# ---------------------------------------------------------------------------
# SearchResult scoring tests
# ---------------------------------------------------------------------------


class TestSearchResultScoring:
    """Test the scoring formula on individual results."""

    def test_vector_only(self):
        r = SearchResult(label="Tech", name="Neo4j", vector_score=0.9)
        r.final_score = 1.0 * r.vector_score + 0.0 * r.fulltext_score
        assert r.final_score == pytest.approx(0.9)

    def test_fulltext_only(self):
        r = SearchResult(label="Tech", name="Neo4j", fulltext_score=0.8)
        r.final_score = 0.0 * r.vector_score + 1.0 * r.fulltext_score
        assert r.final_score == pytest.approx(0.8)

    def test_hybrid_blend(self):
        r = SearchResult(
            label="Tech",
            name="Neo4j",
            vector_score=0.9,
            fulltext_score=0.3,
        )
        alpha = 0.6
        r.final_score = alpha * r.vector_score + (1 - alpha) * r.fulltext_score
        assert r.final_score == pytest.approx(0.66)

    def test_graph_boost_capped(self):
        cfg = HybridConfig(alpha=0.0, graph_beta=0.5, boost_cap=0.3)
        r = SearchResult(
            label="Tech",
            name="Neo4j",
            fulltext_score=0.5,
            graph_boost=1.0,
        )
        r.final_score = (
            cfg.alpha * r.vector_score
            + (1 - cfg.alpha) * r.fulltext_score
            + cfg.graph_beta * min(r.graph_boost, cfg.boost_cap)
        )
        # 0.5 + 0.5*0.3 = 0.65
        assert r.final_score == pytest.approx(0.65)


# ---------------------------------------------------------------------------
# Sync HybridSearchEngine tests (unit)
# ---------------------------------------------------------------------------


class TestHybridSearchSync:
    """Sync search() with mock stores."""

    def test_fulltext_only_when_no_embeddings(self):
        """When embedder has dimensions=0, vector is skipped."""
        graph = _SyncMockGraphStore(
            [
                {"type": "Project", "name": "Engrama", "score": 1.5},
                {"type": "Technology", "name": "Neo4j", "score": 1.2},
            ]
        )
        vector = _SyncNullVectorStore()
        embedder = _NullEmbedder()

        engine = HybridSearchEngine(graph, vector, embedder)
        assert engine.vector_enabled is False

        results = engine.search("engrama", limit=5)
        assert len(results) == 2
        assert results[0].name == "Engrama"
        # With alpha=0 (forced), fulltext_score dominates
        assert results[0].fulltext_score > 0

    def test_hybrid_combines_both_sources(self):
        """When both stores return results, scores are blended."""
        graph = _SyncMockGraphStore(
            [
                {"type": "Project", "name": "Engrama", "score": 1.0},
            ]
        )
        vector = _SyncMockVectorStore(
            [
                {"label": "Project", "name": "Engrama", "score": 0.9},
                {"label": "Technology", "name": "Neo4j", "score": 0.7},
            ]
        )
        embedder = _MockEmbedder()

        engine = HybridSearchEngine(
            graph,
            vector,
            embedder,
            config=HybridConfig(alpha=0.6),
        )
        assert engine.vector_enabled is True

        results = engine.search("engrama", limit=5)
        # Engrama appears in both → blended score
        engrama = next(r for r in results if r.name == "Engrama")
        assert engrama.vector_score > 0
        assert engrama.fulltext_score > 0
        assert engrama.final_score > engrama.vector_score

    def test_empty_results(self):
        """Empty stores return empty results."""
        graph = _SyncMockGraphStore([])
        vector = _SyncMockVectorStore([])
        embedder = _MockEmbedder()

        engine = HybridSearchEngine(graph, vector, embedder)
        results = engine.search("nothing")
        assert results == []

    def test_limit_respected(self):
        """Results are capped at the requested limit."""
        graph = _SyncMockGraphStore(
            [{"type": "Tech", "name": f"Item{i}", "score": 10 - i} for i in range(10)]
        )
        vector = _SyncNullVectorStore()
        embedder = _NullEmbedder()

        engine = HybridSearchEngine(graph, vector, embedder)
        results = engine.search("items", limit=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Temporal scoring fallback when updated_at is missing
# ---------------------------------------------------------------------------


class TestTemporalMissingUpdatedAt:
    """A vector-only hit without ``updated_at`` should fall back to a
    neutral temporal score instead of stealing the recency-at-0-days
    boost (which previously rendered as ``temporal_score=1.0``).
    """

    def _config(self, *, temporal_gamma=0.1):
        # Isolate the temporal signal: zero out vector/fulltext/trust
        # weights so any score difference is attributable to temporal.
        # Linear mode is required for the equal-relevance fixtures — RRF gives
        # equal-score rows consecutive (positional) ranks, so only the linear
        # base (min==max ⇒ 1.0 for all) lets the temporal layer decide order.
        return HybridConfig(
            fusion_mode="linear",
            alpha=0.0,
            graph_beta=0.0,
            temporal_gamma=temporal_gamma,
            trust_delta=0.0,
        )

    def test_missing_updated_at_uses_neutral_score(self):
        # Single fulltext-only hit with no updated_at on the row.
        graph = _SyncMockGraphStore(
            [{"type": "Tech", "name": "A", "score": 1.0}]  # no updated_at
        )
        engine = HybridSearchEngine(
            graph, _SyncNullVectorStore(), _NullEmbedder(), config=self._config()
        )
        results = engine.search("a", limit=5)
        assert len(results) == 1
        assert results[0].temporal_score == DEFAULT_TEMPORAL_SCORE

    def test_temporal_gamma_zero_leaves_dataclass_default(self):
        # When temporal scoring is disabled, the field keeps its
        # dataclass default of 1.0 — the bug only bit when gamma > 0.
        graph = _SyncMockGraphStore([{"type": "Tech", "name": "A", "score": 1.0}])
        engine = HybridSearchEngine(
            graph,
            _SyncNullVectorStore(),
            _NullEmbedder(),
            config=self._config(temporal_gamma=0.0),
        )
        results = engine.search("a", limit=5)
        assert results[0].temporal_score == 1.0

    def test_present_updated_at_is_used(self):
        # Same setup but with updated_at present — the computed score
        # should differ from the neutral fallback (depends on
        # half-life, but we just need to assert "not the fallback").
        from datetime import datetime, timedelta

        old = (datetime.now(UTC) - timedelta(days=365)).isoformat()
        graph = _SyncMockGraphStore(
            [
                {
                    "type": "Tech",
                    "name": "A",
                    "score": 1.0,
                    "updated_at": old,
                    "confidence": 1.0,
                }
            ]
        )
        engine = HybridSearchEngine(
            graph, _SyncNullVectorStore(), _NullEmbedder(), config=self._config()
        )
        results = engine.search("a", limit=5)
        # 365-day-old node with half-life 30d → recency ≈ 2**(-12) ≈ 0.0002.
        assert results[0].temporal_score < DEFAULT_TEMPORAL_SCORE

    def test_stale_hit_outranked_by_unknown_hit_no_more(self):
        # Regression: a node with old updated_at should not be beaten
        # by a node with no updated_at, when everything else is equal.
        from datetime import datetime, timedelta

        old = (datetime.now(UTC) - timedelta(days=365)).isoformat()
        graph = _SyncMockGraphStore(
            [
                {"type": "Tech", "name": "old", "score": 1.0, "updated_at": old},
                {"type": "Tech", "name": "unknown", "score": 1.0},  # no updated_at
            ]
        )
        engine = HybridSearchEngine(
            graph, _SyncNullVectorStore(), _NullEmbedder(), config=self._config()
        )
        results = engine.search("x", limit=5)
        # Both have fulltext_score=1.0 (single-result group normalises to 1.0
        # for everyone, since min == max). The temporal signal therefore
        # decides ranking: "unknown" gets the neutral 0.5 fallback while
        # "old" gets a near-zero recency. So "unknown" must rank above
        # "old" — but only because they're equally relevant, not because
        # "unknown" was treated as today's freshest.
        assert results[0].temporal_score == DEFAULT_TEMPORAL_SCORE
        # The old node still appears, just with a much lower temporal
        # contribution — proving the unknown isn't getting max boost.
        assert results[1].temporal_score < 0.01


# ---------------------------------------------------------------------------
# Async HybridSearchEngine tests (unit)
# ---------------------------------------------------------------------------


class TestHybridSearchAsync:
    """Async asearch() with mock stores."""

    @pytest.mark.asyncio
    async def test_async_fulltext_only(self):
        """Async search falls back to fulltext when no embeddings."""
        graph = _AsyncMockGraphStore(
            [
                {"type": "Project", "name": "Engrama", "score": 1.5},
            ]
        )
        vector = _AsyncNullVectorStore()
        embedder = _NullEmbedder()

        engine = HybridSearchEngine(graph, vector, embedder)
        results = await engine.asearch("engrama", limit=5)
        assert len(results) == 1
        assert results[0].name == "Engrama"

    @pytest.mark.asyncio
    async def test_async_hybrid_combines_both(self):
        """Async search blends vector + fulltext scores."""
        graph = _AsyncMockGraphStore(
            [
                {"type": "Project", "name": "Engrama", "score": 1.0},
            ]
        )
        vector = _AsyncMockVectorStore(
            [
                {"label": "Project", "name": "Engrama", "score": 0.9},
            ]
        )
        embedder = _MockEmbedder()

        engine = HybridSearchEngine(graph, vector, embedder)
        results = await engine.asearch("engrama", limit=5)
        assert len(results) >= 1
        engrama = results[0]
        assert engrama.vector_score > 0
        assert engrama.fulltext_score > 0

    @pytest.mark.asyncio
    async def test_async_empty_results(self):
        """Async empty search returns empty list."""
        graph = _AsyncMockGraphStore([])
        vector = _AsyncMockVectorStore([])
        embedder = _MockEmbedder()

        engine = HybridSearchEngine(graph, vector, embedder)
        results = await engine.asearch("nothing")
        assert results == []

    @pytest.mark.asyncio
    async def test_async_graceful_degradation(self):
        """When vector store raises, falls back to fulltext."""

        class _FailingVectorStore:
            dimensions = 4

            async def search_similar(self, *a, **kw):
                raise RuntimeError("Vector index not ready")

        graph = _AsyncMockGraphStore(
            [
                {"type": "Project", "name": "Engrama", "score": 1.0},
            ]
        )
        vector = _FailingVectorStore()
        embedder = _MockEmbedder()

        engine = HybridSearchEngine(graph, vector, embedder)
        results = await engine.asearch("engrama")
        # Should still return fulltext results despite vector failure
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# last_mode descriptor (issue #17 — silent hybrid degradation)
# ---------------------------------------------------------------------------


class _FailingEmbedder:
    """Embedder whose ``embed``/``aembed`` raises — simulates an
    unreachable Ollama (or any OpenAI-compatible endpoint that's down).
    """

    dimensions = 4

    def embed(self, text: str) -> list[float]:
        raise ConnectionError("ollama not running")

    async def aembed(self, text: str) -> list[float]:
        raise ConnectionError("ollama not running")


class _EmptyEmbedder:
    """Embedder that returns an empty vector without raising — covers
    the silent-failure mode where the provider replies but the body is
    unusable (e.g. malformed JSON, model not loaded).
    """

    dimensions = 4

    def embed(self, text: str) -> list[float]:
        return []

    async def aembed(self, text: str) -> list[float]:
        return []


class TestSearchModeDescriptor:
    """``last_mode`` must distinguish healthy hybrid from silent fallbacks."""

    def test_initial_last_mode_hybrid_when_vector_enabled(self):
        engine = HybridSearchEngine(_SyncMockGraphStore(), _SyncMockVectorStore(), _MockEmbedder())
        assert engine.last_mode == SearchMode(mode="hybrid", degraded=False, reason="")

    def test_initial_last_mode_fulltext_when_vector_disabled(self):
        engine = HybridSearchEngine(_SyncMockGraphStore(), _SyncNullVectorStore(), _NullEmbedder())
        assert engine.last_mode.mode == "fulltext_only"
        assert engine.last_mode.degraded is False

    def test_search_sets_hybrid_mode_on_healthy_run(self):
        engine = HybridSearchEngine(
            _SyncMockGraphStore([{"type": "T", "name": "a", "score": 1.0}]),
            _SyncMockVectorStore([{"label": "T", "name": "a", "score": 0.9}]),
            _MockEmbedder(),
        )
        engine.search("a")
        assert engine.last_mode.mode == "hybrid"
        assert engine.last_mode.degraded is False
        assert engine.last_mode.reason == ""

    def test_search_sets_fulltext_only_when_vector_disabled_by_config(self):
        engine = HybridSearchEngine(
            _SyncMockGraphStore([{"type": "T", "name": "a", "score": 1.0}]),
            _SyncNullVectorStore(),
            _NullEmbedder(),
        )
        engine.search("a")
        assert engine.last_mode.mode == "fulltext_only"
        assert engine.last_mode.degraded is False, "disabled-by-config is not a runtime degradation"

    def test_search_marks_degraded_when_embedder_raises(self):
        engine = HybridSearchEngine(
            _SyncMockGraphStore([{"type": "T", "name": "a", "score": 1.0}]),
            _SyncMockVectorStore(),
            _FailingEmbedder(),
        )
        engine.search("a")
        assert engine.last_mode.mode == "fulltext_only"
        assert engine.last_mode.degraded is True
        assert "ConnectionError" in engine.last_mode.reason
        assert "ollama not running" in engine.last_mode.reason

    def test_search_marks_degraded_when_embedder_returns_empty_vector(self):
        engine = HybridSearchEngine(
            _SyncMockGraphStore([{"type": "T", "name": "a", "score": 1.0}]),
            _SyncMockVectorStore(),
            _EmptyEmbedder(),
        )
        engine.search("a")
        assert engine.last_mode.mode == "fulltext_only"
        assert engine.last_mode.degraded is True
        assert "empty vector" in engine.last_mode.reason

    def test_search_marks_degraded_when_vector_store_raises(self):
        class _FailingVectorStore:
            dimensions = 4

            def search_vectors(self, *a, **kw):
                raise RuntimeError("vec0 table missing")

        engine = HybridSearchEngine(
            _SyncMockGraphStore([{"type": "T", "name": "a", "score": 1.0}]),
            _FailingVectorStore(),
            _MockEmbedder(),
        )
        engine.search("a")
        assert engine.last_mode.mode == "fulltext_only"
        assert engine.last_mode.degraded is True
        assert "RuntimeError" in engine.last_mode.reason

    @pytest.mark.asyncio
    async def test_asearch_sets_hybrid_mode_on_healthy_run(self):
        engine = HybridSearchEngine(
            _AsyncMockGraphStore([{"type": "T", "name": "a", "score": 1.0}]),
            _AsyncMockVectorStore([{"label": "T", "name": "a", "score": 0.9}]),
            _MockEmbedder(),
        )
        await engine.asearch("a")
        assert engine.last_mode.mode == "hybrid"
        assert engine.last_mode.degraded is False

    @pytest.mark.asyncio
    async def test_asearch_marks_degraded_when_embedder_raises(self):
        engine = HybridSearchEngine(
            _AsyncMockGraphStore([{"type": "T", "name": "a", "score": 1.0}]),
            _AsyncMockVectorStore(),
            _FailingEmbedder(),
        )
        await engine.asearch("a")
        assert engine.last_mode.mode == "fulltext_only"
        assert engine.last_mode.degraded is True
        assert "ConnectionError" in engine.last_mode.reason


# ---------------------------------------------------------------------------
# Integration tests (require Neo4j + Ollama)
# ---------------------------------------------------------------------------


def _neo4j_and_ollama_available() -> bool:
    """Check if both Neo4j and Ollama are available."""
    try:
        from engrama.embeddings.ollama import OllamaProvider

        p = OllamaProvider()
        if not p.health_check():
            return False

        from engrama.core.client import EngramaClient

        client = EngramaClient()
        client.verify()
        client.close()
        return True
    except Exception:
        return False


_HYBRID_TEST_SCOPE_PROPS = {"org_id": "test-hybrid", "user_id": "test-hybrid"}


@pytest.mark.skipif(
    not _neo4j_and_ollama_available(),
    reason="Neo4j and/or Ollama not available",
)
class TestHybridSearchIntegration:
    """Integration tests with real Neo4j + Ollama."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_driver):
        """Create stores and seed test data."""
        from engrama.backends.neo4j.backend import Neo4jGraphStore
        from engrama.backends.neo4j.vector import Neo4jVectorStore
        from engrama.core.client import EngramaClient
        from engrama.core.scope import MemoryScope
        from engrama.embeddings.ollama import OllamaProvider
        from engrama.embeddings.text import node_to_text

        self.client = EngramaClient()
        self.graph = Neo4jGraphStore(self.client)
        self.embedder = OllamaProvider()
        self.vector = Neo4jVectorStore(self.client, dimensions=self.embedder.dimensions)
        self.vector.ensure_index()
        # Spec 001: pin the test scope so seed nodes carry identity and the
        # hybrid engine filters by it on read.
        self.scope = MemoryScope(org_id="test-hybrid", user_id="test-hybrid")

        # Seed test nodes with embeddings
        test_nodes = [
            (
                "Technology",
                "name",
                "TestHybridNeo4j",
                {"description": "Graph database", "test": True, **_HYBRID_TEST_SCOPE_PROPS},
            ),
            (
                "Technology",
                "name",
                "TestHybridPython",
                {
                    "description": "Programming language",
                    "test": True,
                    **_HYBRID_TEST_SCOPE_PROPS,
                },
            ),
        ]
        for label, key, value, props in test_nodes:
            self.graph.merge_node(label, key, value, props)
            text = node_to_text(label, {key: value, **props})
            emb = self.embedder.embed(text)
            self.vector.store_vector_by_key(label, key, value, emb)

        yield

        # Cleanup
        self.client.run(
            "MATCH (n) WHERE n.test = true DETACH DELETE n",
            {},
        )
        self.client.close()

    def test_hybrid_search_returns_results(self):
        """Real hybrid search returns results with both score types."""
        engine = HybridSearchEngine(self.graph, self.vector, self.embedder, scope=self.scope)
        results = engine.search("graph database", limit=5)
        names = [r.name for r in results]
        assert "TestHybridNeo4j" in names

    def test_fulltext_fallback_still_works(self):
        """Fulltext-only search (alpha=0) still returns results."""
        from engrama.embeddings.null import NullProvider

        engine = HybridSearchEngine(
            self.graph,
            self.vector,
            NullProvider(),
            scope=self.scope,
        )
        results = engine.search("TestHybridNeo4j", limit=5)
        assert any(r.name == "TestHybridNeo4j" for r in results)


# ---------------------------------------------------------------------------
# T007 — RRF fusion integration (US1). Written first; MUST FAIL until the
# engine is wired to use rrf_fuse as the relevance base (T009/T010): today
# the engine ignores fusion_mode and never populates `rrf_score`.
# ---------------------------------------------------------------------------


class TestRRFFusionIntegration:
    """End-to-end: split lexical/semantic answers both surface, and order is
    stable when one channel's raw scores are rescaled (RG-1)."""

    @staticmethod
    def _engine(fulltext_scale: float = 1.0):
        """A hybrid engine whose two channels carry deliberately mismatched
        scales: vector scores are tiny, fulltext scores are huge (×scale).

        ``Semantic`` is a vector-only answer, ``Lexical`` a fulltext-only
        answer, ``Shared`` ranks #2 in both. RRF (rank-based) must surface
        all three regardless of the scale gap.
        """
        vector = _AsyncMockVectorStore(
            [
                {"label": "Note", "name": "Semantic", "score": 0.0009},
                {"label": "Note", "name": "Shared", "score": 0.0008},
                {"label": "Note", "name": "VTail", "score": 0.0007},
            ]
        )
        graph = _AsyncMockGraphStore(
            [
                {"type": "Note", "name": "Lexical", "score": 5000.0 * fulltext_scale},
                {"type": "Note", "name": "Shared", "score": 3000.0 * fulltext_scale},
                {"type": "Note", "name": "FTail", "score": 1000.0 * fulltext_scale},
            ]
        )
        # Isolate the relevance base: no temporal/trust contribution.
        cfg = HybridConfig(fusion_mode="rrf", temporal_gamma=0.0, trust_delta=0.0)
        return HybridSearchEngine(graph, vector, _MockEmbedder(), config=cfg)

    @pytest.mark.asyncio
    async def test_split_answers_both_surface_and_rrf_score_populated(self):
        """Both the vector-only and fulltext-only answers reach the top, and
        the RRF relevance base is exposed on results (contract §1 / SC-006)."""
        results = await self._engine().asearch("q", limit=5)
        names = [r.name for r in results]

        # The lexical-only and semantic-only answers both surface despite the
        # ~7-orders-of-magnitude scale gap between channels.
        assert "Semantic" in names
        assert "Lexical" in names

        # RRF actually ran: the fused relevance base is populated (non-zero)
        # on the top result. This is what fails today (rrf_score stays 0.0).
        assert results[0].rrf_score > 0.0
        assert all(0.0 <= r.rrf_score <= 1.0 for r in results)

    @pytest.mark.asyncio
    async def test_order_stable_under_raw_score_rescale(self):
        """RG-1: multiplying the fulltext channel's raw scores by a positive
        constant leaves the result order unchanged (rank-based fusion)."""
        base = await self._engine(fulltext_scale=1.0).asearch("q", limit=5)
        rescaled = await self._engine(fulltext_scale=1000.0).asearch("q", limit=5)

        assert [r.name for r in base] == [r.name for r in rescaled]

        # Order stability here must come from the rank-based RRF base, not an
        # incidental property of min-max: the per-name rrf_score map is
        # identical across the rescale (rank-driven), and RRF actually
        # produced signal. (Under min-max the least-relevant tail normalizes
        # to 0.0, so the guarantee is max>0, not all>0.)
        base_rrf = {r.name: r.rrf_score for r in base}
        rescaled_rrf = {r.name: r.rrf_score for r in rescaled}
        assert base_rrf == rescaled_rrf
        assert max(base_rrf.values()) > 0.0


# ---------------------------------------------------------------------------
# T014 — Graph-aware node-distance rerank integration (US2). Written first;
# MUST FAIL until the engine runs the graph stage (T017/T018): today
# `graph_distance_score` stays 0.0 and no neighbours are fetched.
# ---------------------------------------------------------------------------


class _AsyncGraphWithNeighbours:
    """Async graph store mock exposing 1-hop neighbours per node.

    ``fulltext_search`` returns the candidate rows (score order = rank); the
    engine's graph stage calls ``get_node_with_neighbours`` per candidate to
    build the in-window adjacency the cohesion/anchor math runs over.
    """

    def __init__(self, results: list[dict[str, Any]], neighbours: dict[str, list[str]]):
        self._results = results
        self._nbr = neighbours

    async def fulltext_search(self, query: str, limit: int = 10, **kwargs) -> list[dict[str, Any]]:
        return self._results[:limit]

    async def get_neighbours(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []

    async def get_node_with_neighbours(
        self, label: str, key_field: str, key_value: str, hops: int = 1, scope: Any = None
    ) -> dict[str, Any]:
        names = self._nbr.get(key_value, [])
        return {
            "node": {"name": key_value},
            "neighbours": [{"label": "Note", "name": n} for n in names],
        }


class TestGraphRerankIntegration:
    """End-to-end node-distance reranking through the engine."""

    @pytest.mark.asyncio
    async def test_cohesion_lifts_cluster_over_isolated(self):
        """A connected cluster outranks an isolated, higher-relevance node.

        ``iso`` ranks #1 in fulltext (top RRF) but has no neighbour; ``cl1``
        and ``cl2`` are connected. With the graph stage on, cohesion lifts a
        cluster node above ``iso``; the isolated node scores 0 cohesion.
        """
        graph = _AsyncGraphWithNeighbours(
            [
                {"type": "Note", "name": "iso", "score": 3.0},
                {"type": "Note", "name": "cl1", "score": 2.0},
                {"type": "Note", "name": "cl2", "score": 1.0},
            ],
            {"iso": [], "cl1": ["cl2"], "cl2": ["cl1"]},
        )
        cfg = HybridConfig(
            fusion_mode="rrf",
            graph_rerank=True,
            anchor_boost=False,
            graph_beta=2.0,
            temporal_gamma=0.0,
            trust_delta=0.0,
        )
        engine = HybridSearchEngine(graph, _AsyncNullVectorStore(), _NullEmbedder(), config=cfg)
        results = await engine.asearch("q", limit=5)
        by_name = {r.name: r for r in results}

        assert by_name["iso"].graph_distance_score == pytest.approx(0.0)
        assert by_name["cl2"].graph_distance_score > 0.0
        # The clustered node overtakes the isolated top-relevance one.
        assert results[0].name in {"cl1", "cl2"}

    @pytest.mark.asyncio
    async def test_anchor_query_lifts_nodes_near_anchor(self):
        """A query naming an anchor lifts candidates closer to it.

        Chain ANCH–near–far. The query mentions ``ANCH``; the node-distance
        score must rank ``near`` (1 hop) above ``far`` (2 hops).
        """
        graph = _AsyncGraphWithNeighbours(
            [
                {"type": "Note", "name": "far", "score": 3.0},
                {"type": "Note", "name": "near", "score": 2.0},
                {"type": "Note", "name": "ANCH", "score": 1.0},
            ],
            {"ANCH": ["near"], "near": ["ANCH", "far"], "far": ["near"]},
        )
        cfg = HybridConfig(
            fusion_mode="rrf",
            graph_rerank=True,
            anchor_boost=True,
            graph_beta=1.0,
            temporal_gamma=0.0,
            trust_delta=0.0,
        )
        engine = HybridSearchEngine(graph, _AsyncNullVectorStore(), _NullEmbedder(), config=cfg)
        results = await engine.asearch("tell me about ANCH", limit=5)
        by_name = {r.name: r for r in results}

        assert by_name["near"].graph_distance_score > by_name["far"].graph_distance_score


# ---------------------------------------------------------------------------
# T020 / T022 — Reversibility & observability (US3). The composite revert
# (T023) and the bit-for-bit linear branch (T024) already landed with the
# config loader / scoring (T004 / T010), so these lock that behaviour and
# guard against regression rather than driving new code.
# ---------------------------------------------------------------------------


class TestRankingReversibility:
    """`ENGRAMA_RANKING_LEGACY=1` fully reverts to the legacy linear ranking."""

    @staticmethod
    def _rows_and_neighbours():
        rows = [
            {"type": "Note", "name": "a", "score": 3.0},
            {"type": "Note", "name": "b", "score": 2.0},
            {"type": "Note", "name": "c", "score": 1.0},
        ]
        nbr = {"a": ["b"], "b": ["a", "c"], "c": ["b"]}
        return rows, nbr

    def test_legacy_flag_sets_linear_and_disables_graph(self, monkeypatch):
        monkeypatch.setenv("ENGRAMA_RANKING_LEGACY", "1")
        cfg = HybridConfig()
        assert cfg.fusion_mode == "linear"
        assert cfg.graph_rerank is False

    @pytest.mark.asyncio
    async def test_legacy_flag_matches_explicit_linear_byte_for_byte(self, monkeypatch):
        rows, nbr = self._rows_and_neighbours()

        # Revert via the single composite env flag.
        monkeypatch.setenv("ENGRAMA_RANKING_LEGACY", "1")
        eng_legacy = HybridSearchEngine(
            _AsyncGraphWithNeighbours(rows, nbr),
            _AsyncNullVectorStore(),
            _NullEmbedder(),
            config=HybridConfig(),
        )
        legacy = await eng_legacy.asearch("a b c", limit=5)

        # Reference: an explicit pre-feature linear config (no env).
        monkeypatch.delenv("ENGRAMA_RANKING_LEGACY")
        eng_linear = HybridSearchEngine(
            _AsyncGraphWithNeighbours(rows, nbr),
            _AsyncNullVectorStore(),
            _NullEmbedder(),
            config=HybridConfig(fusion_mode="linear", graph_rerank=False),
        )
        linear = await eng_linear.asearch("a b c", limit=5)

        assert [r.name for r in legacy] == [r.name for r in linear]
        assert {r.name: round(r.final_score, 9) for r in legacy} == {
            r.name: round(r.final_score, 9) for r in linear
        }
        # The legacy path leaves the spec-002 signals untouched (no rrf, no
        # graph-distance contribution).
        assert all(r.rrf_score == 0.0 and r.graph_distance_score == 0.0 for r in legacy)


class TestRankingObservability:
    """Every per-signal score is exposed on the result (SC-006)."""

    @pytest.mark.asyncio
    async def test_result_exposes_rrf_and_graph_distance(self):
        graph = _AsyncGraphWithNeighbours(
            [
                {"type": "Note", "name": "x", "score": 2.0},
                {"type": "Note", "name": "y", "score": 1.0},
            ],
            {"x": ["y"], "y": ["x"]},
        )
        cfg = HybridConfig(fusion_mode="rrf", graph_rerank=True)
        engine = HybridSearchEngine(graph, _AsyncNullVectorStore(), _NullEmbedder(), config=cfg)
        results = await engine.asearch("x y", limit=5)

        for r in results:
            # New spec-002 signals sit alongside the existing per-signal scores.
            assert 0.0 <= r.rrf_score <= 1.0
            assert 0.0 <= r.graph_distance_score <= 1.0
            assert hasattr(r, "vector_score")
            assert hasattr(r, "fulltext_score")
            assert hasattr(r, "temporal_score")
            assert hasattr(r, "trust_score")
        # At least one result carries a non-zero fused relevance base.
        assert max(r.rrf_score for r in results) > 0.0


# ---------------------------------------------------------------------------
# T026 — Recall@10: rrf+rerank vs legacy linear (SC-001). A controlled
# multi-hop / cross-channel fixture; the new default must lift recall@10 by
# ≥10% relative over the legacy linear blend. (Real-corpus validation is a
# separate, dataset-dependent benchmark.)
# ---------------------------------------------------------------------------


class TestRecallImprovement:
    """The relevant answers form a connected cluster and appear mid-rank in
    *both* channels; distractors are strong in a *single* channel and
    isolated. Linear (per-channel min-max + alpha) surfaces the loud
    single-channel distractors; rrf rewards cross-channel agreement and
    graph cohesion lifts the cluster — so recall@10 improves."""

    @staticmethod
    def _fixture():
        relevant = [f"r{i}" for i in range(1, 6)]  # 5 relevant, connected
        vec_distractors = [f"dv{i}" for i in range(1, 5)]  # vector-only, loud
        ft_distractors = [f"df{i}" for i in range(1, 5)]  # fulltext-only, loud

        # Vector channel, score-desc: loud distractors first, then relevant
        # mid-rank. Fulltext channel mirrors it with the *other* distractors.
        vector_rows = [
            {"label": "Note", "name": n, "score": s}
            for n, s in [(d, 1.0 - 0.01 * i) for i, d in enumerate(vec_distractors)]
            + [(r, 0.9 - 0.01 * i) for i, r in enumerate(relevant)]
        ]
        fulltext_rows = [
            {"type": "Note", "name": n, "score": s}
            for n, s in [(d, 1.0 - 0.01 * i) for i, d in enumerate(ft_distractors)]
            + [(r, 0.9 - 0.01 * i) for i, r in enumerate(relevant)]
        ]
        # Relevant cluster: clique. Distractors isolated.
        neighbours = {r: [x for x in relevant if x != r] for r in relevant}
        return relevant, vector_rows, fulltext_rows, neighbours

    @staticmethod
    def _recall_at_10(results, relevant) -> float:
        top = {r.name for r in results[:10]}
        return len(top & set(relevant)) / len(relevant)

    @pytest.mark.asyncio
    async def test_rrf_rerank_improves_recall_at_10_by_10pct(self):
        relevant, vec_rows, ft_rows, nbr = self._fixture()

        def _engine(cfg):
            return HybridSearchEngine(
                _AsyncGraphWithNeighbours(ft_rows, nbr),
                _AsyncMockVectorStore(vec_rows),
                _MockEmbedder(),
                config=cfg,
            )

        common = dict(temporal_gamma=0.0, trust_delta=0.0)
        linear = await _engine(
            HybridConfig(fusion_mode="linear", graph_rerank=False, **common)
        ).asearch("q", limit=10)
        rrf = await _engine(HybridConfig(fusion_mode="rrf", graph_rerank=True, **common)).asearch(
            "q", limit=10
        )

        recall_linear = self._recall_at_10(linear, relevant)
        recall_rrf = self._recall_at_10(rrf, relevant)

        # SC-001: ≥10% relative improvement on this multi-hop / cross-channel set.
        assert recall_linear > 0.0
        assert recall_rrf >= 1.10 * recall_linear


# ---------------------------------------------------------------------------
# T030 — Tenancy/security hardening (P9): the graph-rerank neighbour fetch
# must carry the engine's per-request scope, never a process-global one.
# ---------------------------------------------------------------------------


class _ScopeSpyGraph:
    """Records the scope passed to every get_node_with_neighbours call."""

    def __init__(self, results: list[dict[str, Any]]):
        self._results = results
        self.scopes_seen: list[Any] = []

    async def fulltext_search(self, query: str, limit: int = 10, **kwargs) -> list[dict[str, Any]]:
        return self._results[:limit]

    async def get_neighbours(self, *a, **kw) -> list[dict[str, Any]]:
        return []

    async def get_node_with_neighbours(
        self, label: str, key_field: str, key_value: str, hops: int = 1, scope: Any = None
    ) -> dict[str, Any]:
        self.scopes_seen.append(scope)
        return {"node": {"name": key_value}, "neighbours": []}


class TestGraphRerankScopeThreading:
    """Scope is threaded per-request into every neighbour lookup (P9)."""

    @pytest.mark.asyncio
    async def test_neighbour_fetch_uses_per_request_scope(self):
        rows = [
            {"type": "Note", "name": "a", "score": 2.0},
            {"type": "Note", "name": "b", "score": 1.0},
        ]
        request_scope = object()  # an opaque per-request scope token
        graph = _ScopeSpyGraph(rows)
        engine = HybridSearchEngine(
            graph,
            _AsyncNullVectorStore(),
            _NullEmbedder(),
            config=HybridConfig(fusion_mode="rrf", graph_rerank=True),
            scope=request_scope,
        )
        await engine.asearch("a b", limit=5)

        # Every neighbour fetch carried exactly the engine's request scope —
        # not None, not a module-global default.
        assert graph.scopes_seen, "graph rerank never fetched neighbours"
        assert all(s is request_scope for s in graph.scopes_seen)
