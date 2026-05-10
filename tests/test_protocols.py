"""
Tests for Engrama storage protocols.

Verifies that concrete backend implementations satisfy the abstract
protocol interfaces defined in ``engrama.core.protocols``.
"""

from __future__ import annotations

from engrama.backends.neo4j.backend import Neo4jGraphStore
from engrama.backends.neo4j.vector import Neo4jVectorStore
from engrama.backends.null import NullGraphStore, NullVectorStore


class TestGraphStoreProtocol:
    """Verify that graph stores implement the GraphStore protocol."""

    def test_neo4j_graph_store_has_merge_node(self) -> None:
        """Neo4jGraphStore must have a merge_node method."""
        assert hasattr(Neo4jGraphStore, "merge_node")

    def test_neo4j_graph_store_has_get_node(self) -> None:
        assert hasattr(Neo4jGraphStore, "get_node")

    def test_neo4j_graph_store_has_delete_node(self) -> None:
        assert hasattr(Neo4jGraphStore, "delete_node")

    def test_neo4j_graph_store_has_merge_relation(self) -> None:
        assert hasattr(Neo4jGraphStore, "merge_relation")

    def test_neo4j_graph_store_has_get_neighbours(self) -> None:
        assert hasattr(Neo4jGraphStore, "get_neighbours")

    def test_neo4j_graph_store_has_fulltext_search(self) -> None:
        assert hasattr(Neo4jGraphStore, "fulltext_search")

    def test_neo4j_graph_store_has_count_labels(self) -> None:
        assert hasattr(Neo4jGraphStore, "count_labels")

    def test_neo4j_graph_store_has_run_cypher(self) -> None:
        assert hasattr(Neo4jGraphStore, "run_cypher")

    def test_neo4j_graph_store_has_health_check(self) -> None:
        assert hasattr(Neo4jGraphStore, "health_check")

    def test_neo4j_graph_store_has_close(self) -> None:
        assert hasattr(Neo4jGraphStore, "close")

    def test_null_graph_store_has_merge_node(self) -> None:
        assert hasattr(NullGraphStore, "merge_node")

    def test_null_graph_store_has_health_check(self) -> None:
        assert hasattr(NullGraphStore, "health_check")


class TestVectorStoreProtocol:
    """Verify that vector stores have required attributes."""

    def test_neo4j_vector_store_has_dimensions(self) -> None:
        """Neo4jVectorStore instances expose a dimensions attribute."""
        # Can't instantiate without a client, but the class defines it
        assert "dimensions" in Neo4jVectorStore.__init__.__code__.co_varnames

    def test_neo4j_vector_store_has_search_vectors(self) -> None:
        assert hasattr(Neo4jVectorStore, "search_vectors")

    def test_neo4j_vector_store_has_store_vectors(self) -> None:
        assert hasattr(Neo4jVectorStore, "store_vectors")

    def test_neo4j_vector_store_has_count(self) -> None:
        assert hasattr(Neo4jVectorStore, "count")

    def test_null_vector_store_dimensions_is_zero(self) -> None:
        store = NullVectorStore()
        assert store.dimensions == 0

    def test_null_vector_store_search_returns_empty(self) -> None:
        store = NullVectorStore()
        assert store.search_vectors([0.1, 0.2]) == []

    def test_null_vector_store_count_is_zero(self) -> None:
        store = NullVectorStore()
        assert store.count() == 0


class TestNullGraphStore:
    """Tests for the NullGraphStore no-op implementation."""

    def test_merge_node_returns_empty(self) -> None:
        store = NullGraphStore()
        result = store.merge_node("Project", "name", "test", {"status": "active"})
        assert result == []

    def test_get_node_returns_none(self) -> None:
        store = NullGraphStore()
        assert store.get_node("Project", "name", "test") is None

    def test_delete_node_returns_false(self) -> None:
        store = NullGraphStore()
        assert store.delete_node("Project", "name", "test") is False

    def test_merge_relation_returns_empty(self) -> None:
        store = NullGraphStore()
        result = store.merge_relation("Project", "name", "a", "USES", "Technology", "name", "b")
        assert result == []

    def test_get_neighbours_returns_empty(self) -> None:
        store = NullGraphStore()
        assert store.get_neighbours("Project", "name", "test") == []

    def test_fulltext_search_returns_empty(self) -> None:
        store = NullGraphStore()
        assert store.fulltext_search("test") == []

    def test_health_check_returns_ok(self) -> None:
        store = NullGraphStore()
        assert store.health_check() == {"status": "ok", "backend": "null"}

    def test_close_does_not_raise(self) -> None:
        store = NullGraphStore()
        store.close()  # Should not raise


class TestAsyncStoreProtocol:
    """Verify that the async store has all required methods."""

    def test_async_store_has_all_methods(self) -> None:
        from engrama.backends.neo4j.async_store import Neo4jAsyncStore

        required_methods = [
            "merge_node",
            "get_node",
            "delete_node",
            "merge_relation",
            "get_neighbours",
            "get_node_with_neighbours",
            "fulltext_search",
            "count_labels",
            "run_pattern",
            "lookup_node_label",
            "store_embedding",
            "search_similar",
            "delete_embedding",
            "count_embeddings",
            "get_dismissed_titles",
            "get_pending_insights",
            "get_insight_by_title",
            "update_insight_status",
            "mark_insight_synced",
            "find_insight_by_source_query",
            "list_existing_nodes",
            "init_schema",
            "health_check",
        ]
        for method in required_methods:
            assert hasattr(Neo4jAsyncStore, method), f"Missing method: {method}"
