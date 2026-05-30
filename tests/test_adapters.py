"""
Tests for Engrama adapters — MCP tools, relate fix for title-keyed nodes.

Integration tests against a real Neo4j instance.
"""

from __future__ import annotations

import pytest

from engrama.core.client import EngramaClient
from engrama.core.engine import EngramaEngine
from engrama.core.scope import MemoryScope

# Spec 001 T011: engine writes need a complete (org_id, user_id) scope.
_TEST_SCOPE = MemoryScope(org_id="test-adapters", user_id="test-adapters")
# Raw-Cypher node fixtures must carry the same scope, or the now scope-filtered
# merge_relation endpoint match (fail-closed) won't see them (#93).
_SCOPE_CYPHER_PARAMS = {"org_id": "test-adapters", "user_id": "test-adapters"}


@pytest.fixture()
def engine() -> EngramaEngine:
    """Create an EngramaEngine connected to the test Neo4j instance."""
    client = EngramaClient()
    eng = EngramaEngine(client, default_scope=_TEST_SCOPE)
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
            "d.org_id = $org_id, d.user_id = $user_id, "
            "d.created_at = datetime(), d.updated_at = datetime()",
            {"title": "Use Neo4j for memory graph", **_SCOPE_CYPHER_PARAMS},
        )
        neo4j_session.run(
            "MERGE (p:Project {name: $name}) "
            "SET p.test = true, p.status = 'active', "
            "p.org_id = $org_id, p.user_id = $user_id, "
            "p.created_at = datetime(), p.updated_at = datetime()",
            {"name": "TestProject_Relate", **_SCOPE_CYPHER_PARAMS},
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
            "p.org_id = $org_id, p.user_id = $user_id, "
            "p.created_at = datetime(), p.updated_at = datetime()",
            {"title": "Token expiry in auth flow", **_SCOPE_CYPHER_PARAMS},
        )
        neo4j_session.run(
            "MERGE (c:Concept {name: $name}) "
            "SET c.test = true, "
            "c.org_id = $org_id, c.user_id = $user_id, "
            "c.created_at = datetime(), c.updated_at = datetime()",
            {"name": "JWT Authentication", **_SCOPE_CYPHER_PARAMS},
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
            "SET p.test = true, p.org_id = $org_id, p.user_id = $user_id, "
            "p.created_at = datetime(), p.updated_at = datetime()",
            {"name": "TestProject_NameKey", **_SCOPE_CYPHER_PARAMS},
        )
        neo4j_session.run(
            "MERGE (t:Technology {name: $name}) "
            "SET t.test = true, t.org_id = $org_id, t.user_id = $user_id, "
            "t.created_at = datetime(), t.updated_at = datetime()",
            {"name": "Python_TestRelate", **_SCOPE_CYPHER_PARAMS},
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
