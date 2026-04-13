"""
Engrama — Integration tests for Neo4jGraphStore (sync backend).

These tests run against a **real** Neo4j instance (no mocks).
All test nodes are created with ``test=True`` and cleaned up after each test.
"""

from __future__ import annotations

import pytest

from engrama.core.client import EngramaClient
from engrama.backends.neo4j.backend import Neo4jGraphStore


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture(scope="session")
def client() -> EngramaClient:
    """Session-scoped Engrama client connected to the test Neo4j."""
    c = EngramaClient()
    c.verify()
    yield c  # type: ignore[misc]
    c.close()


@pytest.fixture(scope="session")
def store(client: EngramaClient) -> Neo4jGraphStore:
    """Session-scoped Neo4jGraphStore backed by the test client."""
    return Neo4jGraphStore(client)


@pytest.fixture(autouse=True)
def _cleanup_test_nodes(client: EngramaClient) -> None:
    """Delete all nodes marked ``test=True`` after every test."""
    yield  # type: ignore[misc]
    client.run("MATCH (n) WHERE n.test = true DETACH DELETE n")


# ------------------------------------------------------------------
# merge_node
# ------------------------------------------------------------------


class TestMergeNode:
    """Tests for Neo4jGraphStore.merge_node."""

    def test_creates_node_with_timestamps(
        self, store: Neo4jGraphStore, client: EngramaClient,
    ) -> None:
        """A new node should receive created_at and updated_at."""
        store.merge_node(
            "Project", "name", "store-test-alpha",
            {"status": "active", "test": True},
        )
        records = client.run(
            "MATCH (n:Project {name: $name}) RETURN n",
            {"name": "store-test-alpha"},
        )
        assert len(records) == 1
        node = records[0]["n"]
        assert node["created_at"] is not None
        assert node["updated_at"] is not None
        assert node["status"] == "active"

    def test_merge_twice_does_not_duplicate(
        self, store: Neo4jGraphStore, client: EngramaClient,
    ) -> None:
        """Calling merge_node twice must not create two nodes."""
        for _ in range(2):
            store.merge_node(
                "Project", "name", "store-test-beta",
                {"status": "active", "test": True},
            )
        records = client.run(
            "MATCH (n:Project {name: $name}) RETURN count(n) AS cnt",
            {"name": "store-test-beta"},
        )
        assert records[0]["cnt"] == 1

    def test_merge_updates_properties(
        self, store: Neo4jGraphStore, client: EngramaClient,
    ) -> None:
        """Second merge_node should update properties."""
        store.merge_node(
            "Technology", "name", "store-test-gamma",
            {"version": "1.0", "test": True},
        )
        store.merge_node(
            "Technology", "name", "store-test-gamma",
            {"version": "2.0", "test": True},
        )
        records = client.run(
            "MATCH (n:Technology {name: $name}) RETURN n",
            {"name": "store-test-gamma"},
        )
        assert len(records) == 1
        assert records[0]["n"]["version"] == "2.0"


# ------------------------------------------------------------------
# merge_relation
# ------------------------------------------------------------------


class TestMergeRelation:
    """Tests for Neo4jGraphStore.merge_relation."""

    def test_creates_relationship(
        self, store: Neo4jGraphStore, client: EngramaClient,
    ) -> None:
        store.merge_node("Project", "name", "store-rel-proj", {"test": True})
        store.merge_node("Technology", "name", "store-rel-tech", {"test": True})
        result = store.merge_relation(
            "Project", "name", "store-rel-proj",
            "USES",
            "Technology", "name", "store-rel-tech",
        )
        assert len(result) >= 1


# ------------------------------------------------------------------
# fulltext_search
# ------------------------------------------------------------------


class TestFulltextSearch:
    """Tests for Neo4jGraphStore.fulltext_search."""

    def test_search_returns_coalesce_name_title(
        self, store: Neo4jGraphStore,
    ) -> None:
        """BUG-006: fulltext_search must return COALESCE(name, title)."""
        # Create a Decision (title-keyed) node
        store.merge_node(
            "Decision", "title", "store-test-decision-ft",
            {"rationale": "testing fulltext", "test": True},
        )
        results = store.fulltext_search("store-test-decision-ft", limit=5)
        names = [r["name"] for r in results]
        assert "store-test-decision-ft" in names


# ------------------------------------------------------------------
# get_neighbours
# ------------------------------------------------------------------


class TestGetNeighbours:
    """Tests for Neo4jGraphStore.get_neighbours."""

    def test_returns_connected_nodes(
        self, store: Neo4jGraphStore,
    ) -> None:
        store.merge_node("Project", "name", "store-nb-proj", {"test": True})
        store.merge_node("Technology", "name", "store-nb-tech", {"test": True})
        store.merge_relation(
            "Project", "name", "store-nb-proj",
            "USES",
            "Technology", "name", "store-nb-tech",
        )
        results = store.get_neighbours("Project", "name", "store-nb-proj")
        assert len(results) >= 1


# ------------------------------------------------------------------
# count_labels
# ------------------------------------------------------------------


class TestCountLabels:
    """Tests for Neo4jGraphStore.count_labels."""

    def test_returns_correct_counts(
        self, store: Neo4jGraphStore,
    ) -> None:
        store.merge_node("Concept", "name", "store-count-concept", {"test": True})
        counts = store.count_labels()
        assert isinstance(counts, dict)
        # At least one label should have data
        assert sum(counts.values()) >= 1


# ------------------------------------------------------------------
# run_cypher
# ------------------------------------------------------------------


class TestRunCypher:
    """Tests for Neo4jGraphStore.run_cypher."""

    def test_executes_arbitrary_cypher(
        self, store: Neo4jGraphStore,
    ) -> None:
        results = store.run_cypher("RETURN 42 AS answer")
        assert results[0]["answer"] == 42


# ------------------------------------------------------------------
# get_node / delete_node
# ------------------------------------------------------------------


class TestNodeOps:
    """Tests for get_node and delete_node."""

    def test_get_node_returns_properties(
        self, store: Neo4jGraphStore,
    ) -> None:
        store.merge_node(
            "Concept", "name", "store-get-test",
            {"domain": "testing", "test": True},
        )
        node = store.get_node("Concept", "name", "store-get-test")
        assert node is not None
        assert node["domain"] == "testing"

    def test_get_node_returns_none_for_missing(
        self, store: Neo4jGraphStore,
    ) -> None:
        result = store.get_node("Concept", "name", "nonexistent-node-xyz")
        assert result is None

    def test_soft_delete_archives_node(
        self, store: Neo4jGraphStore,
    ) -> None:
        store.merge_node(
            "Concept", "name", "store-del-test",
            {"domain": "testing", "test": True},
        )
        result = store.delete_node("Concept", "name", "store-del-test", soft=True)
        assert result is True
        node = store.get_node("Concept", "name", "store-del-test")
        assert node is not None
        assert node["status"] == "archived"


# ------------------------------------------------------------------
# health_check
# ------------------------------------------------------------------


class TestHealthCheck:
    """Tests for Neo4jGraphStore.health_check."""

    def test_health_check_returns_ok(
        self, store: Neo4jGraphStore,
    ) -> None:
        result = store.health_check()
        assert result["status"] == "ok"
        assert result["backend"] == "neo4j"
