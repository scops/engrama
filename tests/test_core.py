"""
Engrama — Integration tests for the core layer.

These tests run against a **real** Neo4j instance (no mocks).  Start the
database before running::

    docker compose up -d

All test nodes are created with ``test=True`` so that *conftest.py* can
clean them up after each test function.
"""

from __future__ import annotations

import pytest

from engrama.core.client import EngramaClient
from engrama.core.engine import EngramaEngine


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture(scope="session")
def client() -> EngramaClient:
    """Session-scoped Engrama client connected to the test Neo4j.

    Credentials are read from environment variables or ``.env`` file.
    """
    c = EngramaClient()
    c.verify()
    yield c  # type: ignore[misc]
    c.close()


@pytest.fixture(scope="session")
def engine(client: EngramaClient) -> EngramaEngine:
    """Session-scoped Engrama engine backed by the test client."""
    return EngramaEngine(client)


@pytest.fixture(autouse=True)
def _cleanup_test_nodes(client: EngramaClient) -> None:
    """Delete all nodes marked ``test=True`` after every test."""
    yield  # type: ignore[misc]
    client.run("MATCH (n) WHERE n.test = true DETACH DELETE n")


# ------------------------------------------------------------------
# EngramaClient tests
# ------------------------------------------------------------------


class TestEngramaClient:
    """Tests for :class:`EngramaClient`."""

    def test_verify_connects_successfully(self, client: EngramaClient) -> None:
        """``verify()`` should complete without raising when Neo4j is up."""
        # If this raises, the test runner will report a failure.
        client.verify()

    def test_run_returns_records(self, client: EngramaClient) -> None:
        """``run()`` should execute a trivial query and return records."""
        records = client.run("RETURN 1 AS n")
        assert len(records) == 1
        assert records[0]["n"] == 1


# ------------------------------------------------------------------
# EngramaEngine — merge_node
# ------------------------------------------------------------------


class TestMergeNode:
    """Tests for :meth:`EngramaEngine.merge_node`."""

    def test_creates_project_with_timestamps(
        self, engine: EngramaEngine, client: EngramaClient
    ) -> None:
        """A new node should receive both ``created_at`` and ``updated_at``."""
        engine.merge_node(
            "Project",
            {"name": "test-project-alpha", "status": "active", "test": True},
        )

        records = client.run(
            "MATCH (n:Project {name: $name}) RETURN n",
            {"name": "test-project-alpha"},
        )
        assert len(records) == 1
        node = records[0]["n"]
        assert node["created_at"] is not None
        assert node["updated_at"] is not None
        assert node["status"] == "active"
        assert node["test"] is True

    def test_merge_twice_does_not_duplicate(
        self, engine: EngramaEngine, client: EngramaClient
    ) -> None:
        """Calling ``merge_node`` twice with the same name must not create two nodes."""
        for _ in range(2):
            engine.merge_node(
                "Project",
                {"name": "test-project-beta", "status": "active", "test": True},
            )

        records = client.run(
            "MATCH (n:Project {name: $name}) RETURN count(n) AS cnt",
            {"name": "test-project-beta"},
        )
        assert records[0]["cnt"] == 1

    def test_merge_updates_properties(
        self, engine: EngramaEngine, client: EngramaClient
    ) -> None:
        """A second ``merge_node`` call should update properties, not replace the node."""
        engine.merge_node(
            "Technology",
            {"name": "test-tech-gamma", "version": "1.0", "test": True},
        )
        engine.merge_node(
            "Technology",
            {"name": "test-tech-gamma", "version": "2.0", "test": True},
        )

        records = client.run(
            "MATCH (n:Technology {name: $name}) RETURN n",
            {"name": "test-tech-gamma"},
        )
        assert len(records) == 1
        assert records[0]["n"]["version"] == "2.0"

    def test_merge_node_requires_key(self, engine: EngramaEngine) -> None:
        """``merge_node`` must raise if properties lack both ``name`` and ``title``."""
        with pytest.raises(ValueError, match="merge key"):
            engine.merge_node("Project", {"status": "active"})


# ------------------------------------------------------------------
# EngramaEngine — search
# ------------------------------------------------------------------


class TestSearch:
    """Tests for :meth:`EngramaEngine.search`.

    These tests require the ``memory_search`` fulltext index to exist.
    Run ``scripts/init-schema.cypher`` before executing the test suite.
    """

    def test_search_returns_results_after_insert(
        self, engine: EngramaEngine
    ) -> None:
        """Inserting a node then searching for it should yield at least one hit."""
        engine.merge_node(
            "Concept",
            {"name": "test-concept-fulltext-delta", "domain": "testing", "test": True},
        )

        results = engine.search("test-concept-fulltext-delta", limit=5)
        names = [r["name"] for r in results]
        assert "test-concept-fulltext-delta" in names

    def test_search_respects_limit(self, engine: EngramaEngine) -> None:
        """The ``limit`` parameter should cap the number of results."""
        for i in range(5):
            engine.merge_node(
                "Concept",
                {"name": f"test-search-limit-{i}", "domain": "testing", "test": True},
            )

        results = engine.search("test-search-limit", limit=3)
        assert len(results) <= 3


# ------------------------------------------------------------------
# EngramaEngine — get_context
# ------------------------------------------------------------------


class TestGetContext:
    """Tests for :meth:`EngramaEngine.get_context`."""

    def test_returns_connected_nodes(
        self, engine: EngramaEngine
    ) -> None:
        """``get_context`` should traverse relationships and return neighbours."""
        engine.merge_node(
            "Project",
            {"name": "test-ctx-project", "status": "active", "test": True},
        )
        engine.merge_node(
            "Technology",
            {"name": "test-ctx-tech", "type": "framework", "test": True},
        )
        engine.merge_relation(
            from_name="test-ctx-project",
            from_label="Project",
            rel_type="USES",
            to_name="test-ctx-tech",
            to_label="Technology",
        )

        results = engine.get_context("test-ctx-project", "Project", hops=1)
        assert len(results) >= 1

        neighbour_names = [r["neighbour"]["name"] for r in results]
        assert "test-ctx-tech" in neighbour_names

    def test_no_neighbours_returns_empty(self, engine: EngramaEngine) -> None:
        """A node with no relationships should return an empty result set."""
        engine.merge_node(
            "Client",
            {"name": "test-ctx-isolated", "test": True},
        )

        results = engine.get_context("test-ctx-isolated", "Client", hops=1)
        assert results == []
