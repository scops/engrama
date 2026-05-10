"""
Tests for Engrama adapters — MCP tools, relate fix for title-keyed nodes.

Integration tests against a real Neo4j instance.
"""

from __future__ import annotations

import pytest

from engrama.core.client import EngramaClient
from engrama.core.engine import EngramaEngine


@pytest.fixture()
def engine() -> EngramaEngine:
    """Create an EngramaEngine connected to the test Neo4j instance."""
    client = EngramaClient()
    eng = EngramaEngine(client)
    yield eng
    client.close()


class TestRelateWithTitleKeyedNodes:
    """Verify that relate works correctly for Decision/Problem nodes (title key)."""

    def test_relate_decision_to_project(self, engine: EngramaEngine, neo4j_session) -> None:
        """Create a Decision and a Project, relate them with INFORMED_BY,
        and verify the relationship is created.
        """
        # Arrange — create test nodes with test=true for cleanup
        neo4j_session.run(
            "MERGE (d:Decision {title: $title}) "
            "SET d.test = true, d.rationale = 'test rationale', "
            "d.created_at = datetime(), d.updated_at = datetime()",
            {"title": "Use Neo4j for memory graph"},
        )
        neo4j_session.run(
            "MERGE (p:Project {name: $name}) "
            "SET p.test = true, p.status = 'active', "
            "p.created_at = datetime(), p.updated_at = datetime()",
            {"name": "TestProject_Relate"},
        )

        # Act — relate Decision → Project via engine (uses title key for Decision)
        records = engine.merge_relation(
            from_name="TestProject_Relate",
            from_label="Project",
            rel_type="INFORMED_BY",
            to_name="Use Neo4j for memory graph",
            to_label="Decision",
        )

        # Assert — relationship was created
        assert len(records) == 1
        assert records[0]["rel_type"] == "INFORMED_BY"

        # Verify via direct Cypher
        result = neo4j_session.run(
            "MATCH (p:Project {name: $name})-[r:INFORMED_BY]->"
            "(d:Decision {title: $title}) RETURN type(r) AS rel",
            {"name": "TestProject_Relate", "title": "Use Neo4j for memory graph"},
        )
        assert result.single()["rel"] == "INFORMED_BY"

    def test_relate_problem_to_concept(self, engine: EngramaEngine, neo4j_session) -> None:
        """Create a Problem and a Concept, relate them with APPLIES."""
        neo4j_session.run(
            "MERGE (p:Problem {title: $title}) "
            "SET p.test = true, p.status = 'open', "
            "p.created_at = datetime(), p.updated_at = datetime()",
            {"title": "Token expiry in auth flow"},
        )
        neo4j_session.run(
            "MERGE (c:Concept {name: $name}) "
            "SET c.test = true, "
            "c.created_at = datetime(), c.updated_at = datetime()",
            {"name": "JWT Authentication"},
        )

        records = engine.merge_relation(
            from_name="Token expiry in auth flow",
            from_label="Problem",
            rel_type="APPLIES",
            to_name="JWT Authentication",
            to_label="Concept",
        )

        assert len(records) == 1
        assert records[0]["rel_type"] == "APPLIES"

    def test_relate_between_name_keyed_nodes(self, engine: EngramaEngine, neo4j_session) -> None:
        """Standard name-keyed relate still works (regression test)."""
        neo4j_session.run(
            "MERGE (p:Project {name: $name}) "
            "SET p.test = true, p.created_at = datetime(), p.updated_at = datetime()",
            {"name": "TestProject_NameKey"},
        )
        neo4j_session.run(
            "MERGE (t:Technology {name: $name}) "
            "SET t.test = true, t.created_at = datetime(), t.updated_at = datetime()",
            {"name": "Python_TestRelate"},
        )

        records = engine.merge_relation(
            from_name="TestProject_NameKey",
            from_label="Project",
            rel_type="USES",
            to_name="Python_TestRelate",
            to_label="Technology",
        )

        assert len(records) == 1
        assert records[0]["rel_type"] == "USES"
