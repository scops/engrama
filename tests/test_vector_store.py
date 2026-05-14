"""
Tests for Engrama Phase C — vector storage and hybrid search.

Tests are grouped as:
- Neo4jVectorStore: store, search, delete, count (integration — real Neo4j)
- HybridSearchEngine: merge, score, graceful degradation (unit — mocked)
- Engine embed-on-write: verify embedding happens during merge_node
- CLI reindex: command-line smoke test
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from engrama.backends.neo4j.backend import Neo4jGraphStore
from engrama.backends.neo4j.vector import Neo4jVectorStore
from engrama.backends.null import NullVectorStore
from engrama.core.client import EngramaClient
from engrama.core.engine import EngramaEngine
from engrama.core.search import HybridConfig, HybridSearchEngine
from engrama.embeddings.null import NullProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    """Create a real EngramaClient for integration tests."""
    c = EngramaClient()
    c.verify()
    yield c
    c.close()


@pytest.fixture(scope="module")
def graph_store(client):
    """Neo4jGraphStore wrapping the test client."""
    return Neo4jGraphStore(client)


@pytest.fixture(scope="module")
def vector_store(client):
    """Neo4jVectorStore wrapping the test client (768 dims)."""
    vs = Neo4jVectorStore(client, dimensions=768)
    vs.ensure_index()
    return vs


@pytest.fixture(autouse=True)
def cleanup_test_nodes(client):
    """Remove test nodes after each test."""
    yield
    client.run("MATCH (n) WHERE n._test_phase_c = true DETACH DELETE n")


# ---------------------------------------------------------------------------
# Helper: fake embedder
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Deterministic embedder for testing — returns hash-based vectors."""

    dimensions = 768

    def embed(self, text: str) -> list[float]:
        """Return a 768-dim vector derived from the text hash."""
        h = hash(text) % (2**32)
        # Use the hash to seed a pseudo-random sequence
        vec = []
        for i in range(768):
            h = (h * 1103515245 + 12345) & 0xFFFFFFFF
            vec.append((h / 0xFFFFFFFF) * 2 - 1)  # [-1, 1]
        # Normalise to unit length
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def health_check(self) -> bool:
        return True


# ===========================================================================
# Test: Neo4jVectorStore
# ===========================================================================


class TestNeo4jVectorStore:
    """Integration tests for Neo4jVectorStore against real Neo4j."""

    def test_ensure_index_idempotent(self, vector_store):
        """ensure_index can be called multiple times without error."""
        vector_store.ensure_index()
        vector_store.ensure_index()

    def test_store_and_count(self, client, vector_store):
        """Store a vector and verify count increases."""
        # Create a test node
        client.run(
            "CREATE (n:Technology {name: $name, _test_phase_c: true})",
            {"name": "test_vec_tech_1"},
        )
        records = client.run(
            "MATCH (n:Technology {name: $name}) RETURN elementId(n) AS eid",
            {"name": "test_vec_tech_1"},
        )
        eid = records[0]["eid"]

        embedder = FakeEmbedder()
        embedding = embedder.embed("Technology: test_vec_tech_1")

        initial_count = vector_store.count()
        stored = vector_store.store_vectors([(eid, embedding)])
        assert stored == 1
        assert vector_store.count() >= initial_count + 1

        # Verify the :Embedded label was added
        check = client.run(
            "MATCH (n:Embedded {name: $name}) RETURN n",
            {"name": "test_vec_tech_1"},
        )
        assert len(check) == 1

    def test_store_vector_by_key(self, client, vector_store):
        """store_vector_by_key finds a node by label + key and stores embedding."""
        client.run(
            "CREATE (n:Project {name: $name, _test_phase_c: true})",
            {"name": "test_vec_proj_1"},
        )
        embedder = FakeEmbedder()
        embedding = embedder.embed("Project: test_vec_proj_1")

        result = vector_store.store_vector_by_key(
            "Project",
            "name",
            "test_vec_proj_1",
            embedding,
        )
        assert result is True

        # Check :Embedded label
        check = client.run(
            "MATCH (n:Embedded {name: $name}) RETURN n",
            {"name": "test_vec_proj_1"},
        )
        assert len(check) == 1

    def test_store_vector_by_key_not_found(self, vector_store):
        """store_vector_by_key returns False for non-existent node."""
        result = vector_store.store_vector_by_key(
            "Project",
            "name",
            "nonexistent_node_xyz",
            [0.1] * 768,
        )
        assert result is False

    def test_search_vectors(self, client, vector_store):
        """search_vectors finds nodes by embedding similarity."""
        embedder = FakeEmbedder()

        # Create and embed two nodes with different content
        for name in ("test_vec_search_a", "test_vec_search_b"):
            client.run(
                "CREATE (n:Concept {name: $name, _test_phase_c: true})",
                {"name": name},
            )
            emb = embedder.embed(f"Concept: {name}")
            vector_store.store_vector_by_key("Concept", "name", name, emb)

        # Wait a moment for the index to update
        import time

        time.sleep(1)

        # Search with the embedding of node "a" — should find it
        query_emb = embedder.embed("Concept: test_vec_search_a")
        results = vector_store.search_vectors(query_emb, limit=5)
        assert len(results) >= 1

        # Top result should be the matching node
        names = [r["name"] for r in results]
        assert "test_vec_search_a" in names

    def test_delete_vectors(self, client, vector_store):
        """delete_vectors removes embedding and :Embedded label."""
        client.run(
            "CREATE (n:Tool {name: $name, _test_phase_c: true})",
            {"name": "test_vec_delete"},
        )
        records = client.run(
            "MATCH (n:Tool {name: $name}) RETURN elementId(n) AS eid",
            {"name": "test_vec_delete"},
        )
        eid = records[0]["eid"]

        embedder = FakeEmbedder()
        vector_store.store_vectors([(eid, embedder.embed("test"))])

        # Verify embedded
        check = client.run(
            "MATCH (n:Embedded {name: $name}) RETURN n",
            {"name": "test_vec_delete"},
        )
        assert len(check) == 1

        # Delete
        removed = vector_store.delete_vectors([eid])
        assert removed == 1

        # Verify no longer embedded
        check = client.run(
            "MATCH (n:Embedded {name: $name}) RETURN n",
            {"name": "test_vec_delete"},
        )
        assert len(check) == 0

    def test_store_empty_items(self, vector_store):
        """store_vectors with empty list returns 0."""
        assert vector_store.store_vectors([]) == 0

    def test_delete_empty_ids(self, vector_store):
        """delete_vectors with empty list returns 0."""
        assert vector_store.delete_vectors([]) == 0


# ===========================================================================
# Test: HybridSearchEngine
# ===========================================================================


class TestHybridSearchEngine:
    """Unit tests for HybridSearchEngine (mocked backends)."""

    def test_fulltext_only_when_no_embedder(self):
        """When embedder has dimensions=0, search uses fulltext only."""
        graph = MagicMock()
        graph.fulltext_search.return_value = [
            {"type": "Technology", "name": "Python", "score": 5.0},
            {"type": "Technology", "name": "Java", "score": 3.0},
        ]
        vector = NullVectorStore()
        embedder = NullProvider()

        engine = HybridSearchEngine(graph, vector, embedder)
        assert engine.vector_enabled is False

        results = engine.search("python", limit=5)
        assert len(results) == 2
        assert results[0].name == "Python"
        # Alpha forced to 0, so final_score = fulltext_score
        assert results[0].vector_score == 0.0
        assert results[0].fulltext_score > 0.0

    def test_hybrid_merge_results(self):
        """Both vector and fulltext results are merged correctly."""
        graph = MagicMock()
        graph.fulltext_search.return_value = [
            {"type": "Technology", "name": "Neo4j", "score": 4.0},
            {"type": "Technology", "name": "Python", "score": 2.0},
        ]
        vector = MagicMock()
        vector.dimensions = 768
        vector.search_vectors.return_value = [
            {"node_id": "eid1", "label": "Technology", "name": "Neo4j", "score": 0.95},
            {"node_id": "eid2", "label": "Concept", "name": "Graph DB", "score": 0.80},
        ]
        embedder = MagicMock()
        embedder.dimensions = 768
        embedder.embed.return_value = [0.1] * 768

        engine = HybridSearchEngine(graph, vector, embedder)
        assert engine.vector_enabled is True

        results = engine.search("graph database", limit=5)
        names = [r.name for r in results]

        # Neo4j should be top — appears in both result sets
        assert "Neo4j" in names
        assert "Python" in names
        assert "Graph DB" in names

        # Neo4j should have both scores
        neo4j_r = next(r for r in results if r.name == "Neo4j")
        assert neo4j_r.vector_score > 0
        assert neo4j_r.fulltext_score > 0

    def test_vector_failure_falls_back(self):
        """When embed() raises, engine falls back to fulltext."""
        graph = MagicMock()
        graph.fulltext_search.return_value = [
            {"type": "Project", "name": "engrama", "score": 5.0},
        ]
        vector = MagicMock()
        vector.dimensions = 768
        embedder = MagicMock()
        embedder.dimensions = 768
        embedder.embed.side_effect = ConnectionError("Ollama down")

        engine = HybridSearchEngine(graph, vector, embedder)
        results = engine.search("engrama")
        assert len(results) == 1
        assert results[0].name == "engrama"
        # Vector score should be 0 (fallback)
        assert results[0].vector_score == 0.0

    def test_custom_config(self):
        """HybridConfig alpha is respected."""
        graph = MagicMock()
        graph.fulltext_search.return_value = [
            {"type": "Project", "name": "A", "score": 5.0},
        ]
        vector = MagicMock()
        vector.dimensions = 768
        vector.search_vectors.return_value = [
            {"node_id": "x", "label": "Project", "name": "A", "score": 0.9},
        ]
        embedder = MagicMock()
        embedder.dimensions = 768
        embedder.embed.return_value = [0.1] * 768

        # Vector only — zero out every other signal so final == vector.
        config = HybridConfig(alpha=1.0, temporal_gamma=0.0, trust_delta=0.0)
        engine = HybridSearchEngine(graph, vector, embedder, config)
        results = engine.search("test")
        assert len(results) == 1
        assert results[0].final_score == results[0].vector_score

    def test_empty_results(self):
        """Both backends returning empty gives empty results."""
        graph = MagicMock()
        graph.fulltext_search.return_value = []
        vector = MagicMock()
        vector.dimensions = 768
        vector.search_vectors.return_value = []
        embedder = MagicMock()
        embedder.dimensions = 768
        embedder.embed.return_value = [0.1] * 768

        engine = HybridSearchEngine(graph, vector, embedder)
        results = engine.search("nothing")
        assert results == []


# ===========================================================================
# Test: Engine embed-on-write
# ===========================================================================


class TestEngineEmbedOnWrite:
    """Verify that engine.merge_node embeds when embedder is configured."""

    def test_embed_on_write_calls_embedder(self):
        """merge_node calls embedder.embed when embed_on_write is active."""
        store = MagicMock()
        store.merge_node.return_value = []
        vector = MagicMock()
        vector.dimensions = 768
        vector.store_vector_by_key = MagicMock(return_value=True)
        embedder = MagicMock()
        embedder.dimensions = 768
        embedder.embed.return_value = [0.1] * 768

        engine = EngramaEngine(store, vector_store=vector, embedder=embedder)
        assert engine._embed_on_write is True

        engine.merge_node("Technology", {"name": "Python", "description": "Language"})

        embedder.embed.assert_called_once()
        vector.store_vector_by_key.assert_called_once_with(
            "Technology",
            "name",
            "Python",
            [0.1] * 768,
        )

    def test_no_embed_when_null_provider(self):
        """merge_node does NOT embed when NullProvider is configured."""
        store = MagicMock()
        store.merge_node.return_value = []
        vector = NullVectorStore()
        embedder = NullProvider()

        engine = EngramaEngine(store, vector_store=vector, embedder=embedder)
        assert engine._embed_on_write is False

        engine.merge_node("Technology", {"name": "Python"})
        # No embedding call — NullProvider has dimensions=0

    def test_embed_failure_does_not_break_merge(self):
        """If embedder.embed raises, merge_node still succeeds."""
        store = MagicMock()
        store.merge_node.return_value = [{"n": {"name": "Python"}}]
        vector = MagicMock()
        vector.dimensions = 768
        embedder = MagicMock()
        embedder.dimensions = 768
        embedder.embed.side_effect = ConnectionError("Ollama down")

        engine = EngramaEngine(store, vector_store=vector, embedder=embedder)
        result = engine.merge_node("Technology", {"name": "Python"})
        # merge_node should still return the result
        assert result == [{"n": {"name": "Python"}}]

    def test_hybrid_search_method(self):
        """engine.hybrid_search returns SearchResult objects."""
        store = MagicMock()
        store.fulltext_search.return_value = [
            {"type": "Technology", "name": "Neo4j", "score": 3.0},
        ]
        vector = MagicMock()
        vector.dimensions = 768
        vector.search_vectors.return_value = []
        embedder = MagicMock()
        embedder.dimensions = 768
        embedder.embed.return_value = [0.1] * 768

        engine = EngramaEngine(store, vector_store=vector, embedder=embedder)
        results = engine.hybrid_search("neo4j")
        assert len(results) >= 1
        assert results[0].name == "Neo4j"

    def test_hybrid_search_fallback_no_embedder(self):
        """hybrid_search falls back to fulltext when no embedder."""
        store = MagicMock()
        store.fulltext_search.return_value = [
            {"type": "Technology", "name": "Neo4j", "score": 3.0},
        ]

        engine = EngramaEngine(store)
        results = engine.hybrid_search("neo4j")
        assert len(results) == 1
        assert results[0].name == "Neo4j"
        assert results[0].fulltext_score == 3.0


# ===========================================================================
# Test: Backend factory
# ===========================================================================


class TestBackendFactory:
    """Test that create_stores returns correct vector store."""

    def test_neo4j_vector_backend(self, client):
        """VECTOR_BACKEND=neo4j returns Neo4jVectorStore."""
        from engrama.backends import _create_vector_store
        from engrama.backends.neo4j.backend import Neo4jGraphStore

        graph = Neo4jGraphStore(client)
        with patch.dict(os.environ, {"EMBEDDING_DIMENSIONS": "768"}):
            vs = _create_vector_store("neo4j", {}, graph)
        assert isinstance(vs, Neo4jVectorStore)
        assert vs.dimensions == 768

    def test_none_vector_backend(self):
        """VECTOR_BACKEND=none returns NullVectorStore."""
        from engrama.backends import _create_vector_store

        vs = _create_vector_store("none", {}, None)
        assert isinstance(vs, NullVectorStore)

    def test_neo4j_without_graph_raises(self):
        """VECTOR_BACKEND=neo4j without a graph store raises ValueError."""
        from engrama.backends import _create_vector_store

        graph = MagicMock()
        graph._client = None
        del graph._client
        with pytest.raises(ValueError, match="requires GRAPH_BACKEND=neo4j"):
            _create_vector_store("neo4j", {}, graph)
