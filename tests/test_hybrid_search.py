"""
Tests for the hybrid search engine (DDR-003 Phase C).

Unit tests use mock stores (no external dependencies).
Integration tests hit real Neo4j + Ollama (skipped if unavailable).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from engrama.core.search import HybridConfig, HybridSearchEngine, SearchResult


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

    def fulltext_search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return self._results[:limit]

    def get_neighbours(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []


class _AsyncMockGraphStore:
    """Fake graph store for async tests (coroutine return values)."""

    def __init__(self, results: list[dict[str, Any]] | None = None):
        self._results = results or []

    async def fulltext_search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return self._results[:limit]

    async def get_neighbours(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []


class _SyncMockVectorStore:
    """Fake vector store for sync tests."""

    dimensions = 4

    def __init__(self, results: list[dict[str, Any]] | None = None):
        self._results = results or []

    def search_vectors(
        self, query_embedding: list[float], limit: int = 10, scope: Any = None,
    ) -> list[dict[str, Any]]:
        return self._results[:limit]


class _AsyncMockVectorStore:
    """Fake vector store for async tests."""

    dimensions = 4

    def __init__(self, results: list[dict[str, Any]] | None = None):
        self._results = results or []

    async def search_similar(
        self, query_embedding: list[float], limit: int = 10,
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
            label="Tech", name="Neo4j",
            vector_score=0.9, fulltext_score=0.3,
        )
        alpha = 0.6
        r.final_score = alpha * r.vector_score + (1 - alpha) * r.fulltext_score
        assert r.final_score == pytest.approx(0.66)

    def test_graph_boost_capped(self):
        cfg = HybridConfig(alpha=0.0, graph_beta=0.5, boost_cap=0.3)
        r = SearchResult(
            label="Tech", name="Neo4j",
            fulltext_score=0.5, graph_boost=1.0,
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
        graph = _SyncMockGraphStore([
            {"type": "Project", "name": "Engrama", "score": 1.5},
            {"type": "Technology", "name": "Neo4j", "score": 1.2},
        ])
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
        graph = _SyncMockGraphStore([
            {"type": "Project", "name": "Engrama", "score": 1.0},
        ])
        vector = _SyncMockVectorStore([
            {"label": "Project", "name": "Engrama", "score": 0.9},
            {"label": "Technology", "name": "Neo4j", "score": 0.7},
        ])
        embedder = _MockEmbedder()

        engine = HybridSearchEngine(
            graph, vector, embedder,
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
        graph = _SyncMockGraphStore([
            {"type": "Tech", "name": f"Item{i}", "score": 10 - i}
            for i in range(10)
        ])
        vector = _SyncNullVectorStore()
        embedder = _NullEmbedder()

        engine = HybridSearchEngine(graph, vector, embedder)
        results = engine.search("items", limit=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Async HybridSearchEngine tests (unit)
# ---------------------------------------------------------------------------


class TestHybridSearchAsync:
    """Async asearch() with mock stores."""

    @pytest.mark.asyncio
    async def test_async_fulltext_only(self):
        """Async search falls back to fulltext when no embeddings."""
        graph = _AsyncMockGraphStore([
            {"type": "Project", "name": "Engrama", "score": 1.5},
        ])
        vector = _AsyncNullVectorStore()
        embedder = _NullEmbedder()

        engine = HybridSearchEngine(graph, vector, embedder)
        results = await engine.asearch("engrama", limit=5)
        assert len(results) == 1
        assert results[0].name == "Engrama"

    @pytest.mark.asyncio
    async def test_async_hybrid_combines_both(self):
        """Async search blends vector + fulltext scores."""
        graph = _AsyncMockGraphStore([
            {"type": "Project", "name": "Engrama", "score": 1.0},
        ])
        vector = _AsyncMockVectorStore([
            {"label": "Project", "name": "Engrama", "score": 0.9},
        ])
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

        graph = _AsyncMockGraphStore([
            {"type": "Project", "name": "Engrama", "score": 1.0},
        ])
        vector = _FailingVectorStore()
        embedder = _MockEmbedder()

        engine = HybridSearchEngine(graph, vector, embedder)
        results = await engine.asearch("engrama")
        # Should still return fulltext results despite vector failure
        assert len(results) >= 1


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


@pytest.mark.skipif(
    not _neo4j_and_ollama_available(),
    reason="Neo4j and/or Ollama not available",
)
class TestHybridSearchIntegration:
    """Integration tests with real Neo4j + Ollama."""

    @pytest.fixture(autouse=True)
    def setup(self, neo4j_driver):
        """Create stores and seed test data."""
        from engrama.core.client import EngramaClient
        from engrama.backends.neo4j.backend import Neo4jGraphStore
        from engrama.backends.neo4j.vector import Neo4jVectorStore
        from engrama.embeddings.ollama import OllamaProvider
        from engrama.embeddings.text import node_to_text

        self.client = EngramaClient()
        self.graph = Neo4jGraphStore(self.client)
        self.embedder = OllamaProvider()
        self.vector = Neo4jVectorStore(self.client, dimensions=self.embedder.dimensions)
        self.vector.ensure_index()

        # Seed test nodes with embeddings
        test_nodes = [
            ("Technology", "name", "TestHybridNeo4j", {"description": "Graph database", "test": True}),
            ("Technology", "name", "TestHybridPython", {"description": "Programming language", "test": True}),
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
        engine = HybridSearchEngine(self.graph, self.vector, self.embedder)
        results = engine.search("graph database", limit=5)
        names = [r.name for r in results]
        assert "TestHybridNeo4j" in names

    def test_fulltext_fallback_still_works(self):
        """Fulltext-only search (alpha=0) still returns results."""
        from engrama.embeddings.null import NullProvider

        engine = HybridSearchEngine(
            self.graph, self.vector, NullProvider(),
        )
        results = engine.search("TestHybridNeo4j", limit=5)
        assert any(r.name == "TestHybridNeo4j" for r in results)
