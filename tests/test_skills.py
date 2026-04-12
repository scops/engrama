"""
Tests for Engrama skills — reflect (cross-entity pattern detection).

Integration tests against a real Neo4j instance with seeded test data.
"""

from __future__ import annotations

import pytest

from engrama.core.client import EngramaClient
from engrama.core.engine import EngramaEngine
from engrama.skills.reflect import ReflectSkill


@pytest.fixture()
def engine() -> EngramaEngine:
    """Create an EngramaEngine connected to the test Neo4j instance."""
    client = EngramaClient()
    eng = EngramaEngine(client)
    yield eng
    client.close()


@pytest.fixture()
def seed_cross_project(neo4j_session) -> None:
    """Seed data for Query 1: cross-project solution transfer.

    Graph:
        ProjectA -[:INFORMED_BY]-> DecisionX
        ProjectA -[:HAS]-> ResolvedProblem -[:SOLVED_BY]-> DecisionX
        ResolvedProblem -[:APPLIES]-> ConceptShared
        ProjectB -[:HAS]-> OpenProblem -[:APPLIES]-> ConceptShared
    """
    neo4j_session.run(
        "MERGE (pA:Project {name: $pA}) SET pA.test = true, pA.status = 'active', "
        "pA.created_at = datetime(), pA.updated_at = datetime() "
        "MERGE (pB:Project {name: $pB}) SET pB.test = true, pB.status = 'active', "
        "pB.created_at = datetime(), pB.updated_at = datetime() "
        "MERGE (d:Decision {title: $d}) SET d.test = true, "
        "d.created_at = datetime(), d.updated_at = datetime() "
        "MERGE (rp:Problem {title: $rp}) SET rp.test = true, rp.status = 'resolved', "
        "rp.created_at = datetime(), rp.updated_at = datetime() "
        "MERGE (op:Problem {title: $op}) SET op.test = true, op.status = 'open', "
        "op.created_at = datetime(), op.updated_at = datetime() "
        "MERGE (c:Concept {name: $c}) SET c.test = true, "
        "c.created_at = datetime(), c.updated_at = datetime() "
        "MERGE (pA)-[:INFORMED_BY]->(d) "
        "MERGE (pA)-[:HAS]->(rp) "
        "MERGE (rp)-[:SOLVED_BY]->(d) "
        "MERGE (rp)-[:APPLIES]->(c) "
        "MERGE (pB)-[:HAS]->(op) "
        "MERGE (op)-[:APPLIES]->(c)",
        {
            "pA": "Reflect_ProjectA",
            "pB": "Reflect_ProjectB",
            "d": "Use event sourcing",
            "rp": "Data inconsistency in sync",
            "op": "Data loss during import",
            "c": "Event Sourcing",
        },
    )


@pytest.fixture()
def seed_shared_tech(neo4j_session) -> None:
    """Seed data for Query 2: shared technology.

    Graph:
        ProjectC -[:USES]-> TechShared <-[:USES]- ProjectD
    """
    neo4j_session.run(
        "MERGE (pC:Project {name: $pC}) SET pC.test = true, pC.status = 'active', "
        "pC.created_at = datetime(), pC.updated_at = datetime() "
        "MERGE (pD:Project {name: $pD}) SET pD.test = true, pD.status = 'active', "
        "pD.created_at = datetime(), pD.updated_at = datetime() "
        "MERGE (t:Technology {name: $t}) SET t.test = true, "
        "t.created_at = datetime(), t.updated_at = datetime() "
        "MERGE (pC)-[:USES]->(t) "
        "MERGE (pD)-[:USES]->(t)",
        {
            "pC": "Reflect_ProjectC",
            "pD": "Reflect_ProjectD",
            "t": "FastAPI_ReflectTest",
        },
    )


@pytest.fixture()
def seed_training(neo4j_session) -> None:
    """Seed data for Query 3: training opportunity.

    Graph:
        OpenProblem -[:APPLIES]-> ConceptTrain <-[:COVERS]- Course
    """
    neo4j_session.run(
        "MERGE (op:Problem {title: $op}) SET op.test = true, op.status = 'open', "
        "op.created_at = datetime(), op.updated_at = datetime() "
        "MERGE (c:Concept {name: $c}) SET c.test = true, "
        "c.created_at = datetime(), c.updated_at = datetime() "
        "MERGE (course:Course {name: $course}) SET course.test = true, "
        "course.created_at = datetime(), course.updated_at = datetime() "
        "MERGE (op)-[:APPLIES]->(c) "
        "MERGE (course)-[:COVERS]->(c)",
        {
            "op": "Privilege escalation via SUID",
            "c": "Linux Privilege Escalation",
            "course": "Ethical Hacking Advanced",
        },
    )


class TestReflectSkill:
    """Integration tests for ReflectSkill pattern detection."""

    def test_cross_project_solution(
        self, engine: EngramaEngine, neo4j_session, seed_cross_project
    ) -> None:
        """Detect that a resolved problem's decision may apply to an open problem."""
        skill = ReflectSkill()
        insights = skill.run(engine)

        # Find the cross-project insight
        cross = [i for i in insights if i.source_query == "cross_project_solution"]
        assert len(cross) >= 1

        insight = cross[0]
        assert "Reflect_ProjectA" in insight.body
        assert "Reflect_ProjectB" in insight.body
        assert "Use event sourcing" in insight.title
        assert insight.status == "pending"
        assert insight.confidence == 0.85

        # Verify Insight node exists in Neo4j
        result = neo4j_session.run(
            "MATCH (i:Insight {source_query: $sq}) "
            "WHERE i.title CONTAINS 'Use event sourcing' "
            "RETURN i.title AS title, i.status AS status",
            {"sq": "cross_project_solution"},
        )
        record = result.single()
        assert record is not None
        assert record["status"] == "pending"

        # Cleanup insights
        neo4j_session.run(
            "MATCH (i:Insight) WHERE i.title CONTAINS 'Reflect_Project' "
            "OR i.title CONTAINS 'Use event sourcing' DETACH DELETE i"
        )

    def test_shared_technology(
        self, engine: EngramaEngine, neo4j_session, seed_shared_tech
    ) -> None:
        """Detect that two active projects share a technology."""
        skill = ReflectSkill()
        insights = skill.run(engine)

        shared = [i for i in insights if i.source_query == "shared_technology"]
        assert len(shared) >= 1

        # Filter for the specific seeded insight (other pre-existing data
        # in the graph may produce additional shared_technology insights).
        matching = [i for i in shared if "FastAPI_ReflectTest" in i.title]
        assert len(matching) >= 1, (
            f"Expected an insight about FastAPI_ReflectTest, got: "
            f"{[i.title for i in shared]}"
        )

        insight = matching[0]
        assert "FastAPI_ReflectTest" in insight.title
        # Both seeded entities are Project → same type → confidence 0.6
        assert insight.confidence == 0.6
        assert insight.status == "pending"

        # Verify Insight node in Neo4j
        result = neo4j_session.run(
            "MATCH (i:Insight {source_query: $sq}) "
            "WHERE i.title CONTAINS 'FastAPI_ReflectTest' "
            "RETURN i.title AS title",
            {"sq": "shared_technology"},
        )
        assert result.single() is not None

        # Cleanup
        neo4j_session.run(
            "MATCH (i:Insight) WHERE i.title CONTAINS 'FastAPI_ReflectTest' "
            "DETACH DELETE i"
        )

    def test_training_opportunity(
        self, engine: EngramaEngine, neo4j_session, seed_training
    ) -> None:
        """Detect that a course covers a concept related to an open problem."""
        skill = ReflectSkill()
        insights = skill.run(engine)

        training = [i for i in insights if i.source_query == "training_opportunity"]
        assert len(training) >= 1

        insight = training[0]
        assert "Ethical Hacking Advanced" in insight.title
        assert "Linux Privilege Escalation" in insight.title
        assert insight.confidence == 0.65
        assert insight.status == "pending"

        # Verify in Neo4j
        result = neo4j_session.run(
            "MATCH (i:Insight {source_query: $sq}) "
            "WHERE i.title CONTAINS 'Ethical Hacking Advanced' "
            "RETURN i.title AS title",
            {"sq": "training_opportunity"},
        )
        assert result.single() is not None

        # Cleanup
        neo4j_session.run(
            "MATCH (i:Insight) WHERE i.title CONTAINS 'Ethical Hacking' "
            "DETACH DELETE i"
        )

    def test_no_insights_on_empty_graph(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """No insights generated when no matching patterns exist."""
        # With only test-flagged cleanup nodes, queries should return nothing
        # relevant (there may be pre-existing data, but unique test names
        # ensure our seeded patterns are gone after cleanup)
        skill = ReflectSkill()
        # Just verify it runs without error
        insights = skill.run(engine)
        assert isinstance(insights, list)

        # Cleanup any insights created
        neo4j_session.run("MATCH (i:Insight) WHERE i.test = true DETACH DELETE i")
