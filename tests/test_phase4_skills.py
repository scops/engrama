"""
Tests for Engrama Phase 4 skills: remember, recall, associate, forget.

Integration tests against a real Neo4j instance.
"""

from __future__ import annotations

import pytest

from engrama.core.client import EngramaClient
from engrama.core.engine import EngramaEngine
from engrama.skills.remember import RememberSkill
from engrama.skills.recall import RecallSkill
from engrama.skills.associate import AssociateSkill
from engrama.skills.forget import ForgetSkill


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> EngramaEngine:
    """Create an EngramaEngine connected to the test Neo4j instance."""
    client = EngramaClient()
    eng = EngramaEngine(client)
    yield eng
    client.close()


@pytest.fixture()
def seed_recall_data(neo4j_session) -> None:
    """Seed a small graph for recall tests.

    Graph:
        P4_ProjectAlpha -[:USES]-> P4_FastAPI
        P4_ProjectAlpha -[:HAS]-> P4_MemoryLeak (open problem)
        P4_MemoryLeak -[:APPLIES]-> P4_Caching
    """
    neo4j_session.run(
        "MERGE (p:Project {name: $proj}) "
        "SET p.test = true, p.status = 'active', p.description = 'Recall test project', "
        "    p.created_at = datetime(), p.updated_at = datetime() "
        "MERGE (t:Technology {name: $tech}) "
        "SET t.test = true, t.created_at = datetime(), t.updated_at = datetime() "
        "MERGE (prob:Problem {title: $prob}) "
        "SET prob.test = true, prob.status = 'open', "
        "    prob.created_at = datetime(), prob.updated_at = datetime() "
        "MERGE (c:Concept {name: $concept}) "
        "SET c.test = true, c.created_at = datetime(), c.updated_at = datetime() "
        "MERGE (p)-[:USES]->(t) "
        "MERGE (p)-[:HAS]->(prob) "
        "MERGE (prob)-[:APPLIES]->(c)",
        {
            "proj": "P4_ProjectAlpha",
            "tech": "P4_FastAPI",
            "prob": "P4_MemoryLeak in worker pool",
            "concept": "P4_Caching",
        },
    )


@pytest.fixture()
def seed_old_node(neo4j_session) -> None:
    """Seed a node with an old updated_at for TTL archive test."""
    neo4j_session.run(
        "MERGE (t:Technology {name: $name}) "
        "SET t.test = true, "
        "    t.created_at = datetime() - duration({days: 400}), "
        "    t.updated_at = datetime() - duration({days: 400})",
        {"name": "P4_ObsoleteTech"},
    )


@pytest.fixture()
def seed_old_node_purge(neo4j_session) -> None:
    """Seed a node with an old updated_at for TTL purge test."""
    neo4j_session.run(
        "MERGE (t:Technology {name: $name}) "
        "SET t.test = true, "
        "    t.created_at = datetime() - duration({days: 400}), "
        "    t.updated_at = datetime() - duration({days: 400})",
        {"name": "P4_ObsoleteTechPurge"},
    )


# ===========================================================================
# RememberSkill
# ===========================================================================


class TestRememberSkill:
    """Integration tests for the remember skill."""

    def test_remember_creates_new_node(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Remember creates a new node when it doesn't exist."""
        skill = RememberSkill()
        result = skill.run(
            engine,
            label="Technology",
            name="P4_TestRememberTech",
            observation="A test technology for remember skill",
        )
        assert result["label"] == "Technology"
        assert result["name"] == "P4_TestRememberTech"
        assert result["key"] == "name"
        assert result["created"] is True

        # Verify in Neo4j
        rec = neo4j_session.run(
            "MATCH (n:Technology {name: $name}) RETURN n.notes AS notes",
            {"name": "P4_TestRememberTech"},
        ).single()
        assert rec is not None
        assert rec["notes"] == "A test technology for remember skill"

        # Cleanup
        neo4j_session.run(
            "MATCH (n:Technology {name: 'P4_TestRememberTech'}) DETACH DELETE n"
        )

    def test_remember_updates_existing_node(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Remember updates notes on an existing node."""
        skill = RememberSkill()

        # Create first
        skill.run(
            engine, label="Concept", name="P4_TestConcept",
            observation="Initial observation",
        )
        # Update
        result = skill.run(
            engine, label="Concept", name="P4_TestConcept",
            observation="Updated observation",
        )
        assert result["created"] is False

        rec = neo4j_session.run(
            "MATCH (n:Concept {name: $name}) RETURN n.notes AS notes",
            {"name": "P4_TestConcept"},
        ).single()
        assert rec["notes"] == "Updated observation"

        # Cleanup
        neo4j_session.run(
            "MATCH (n:Concept {name: 'P4_TestConcept'}) DETACH DELETE n"
        )

    def test_remember_title_keyed_node(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Remember correctly uses 'title' as key for Decision nodes."""
        skill = RememberSkill()
        result = skill.run(
            engine, label="Decision", name="P4_Use microservices",
            observation="Decided for scalability reasons",
        )
        assert result["key"] == "title"
        assert result["created"] is True

        rec = neo4j_session.run(
            "MATCH (n:Decision {title: $title}) RETURN n.notes AS notes",
            {"title": "P4_Use microservices"},
        ).single()
        assert rec is not None
        assert rec["notes"] == "Decided for scalability reasons"

        # Cleanup
        neo4j_session.run(
            "MATCH (n:Decision {title: 'P4_Use microservices'}) DETACH DELETE n"
        )

    def test_remember_with_extra_properties(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Remember passes extra properties through to the node."""
        skill = RememberSkill()
        skill.run(
            engine, label="Project", name="P4_ExtraPropsProject",
            observation="Testing extra props",
            extra={"status": "active", "repo": "github.com/test"},
        )

        rec = neo4j_session.run(
            "MATCH (n:Project {name: $name}) "
            "RETURN n.status AS status, n.repo AS repo, n.notes AS notes",
            {"name": "P4_ExtraPropsProject"},
        ).single()
        assert rec["status"] == "active"
        assert rec["repo"] == "github.com/test"
        assert rec["notes"] == "Testing extra props"

        # Cleanup
        neo4j_session.run(
            "MATCH (n:Project {name: 'P4_ExtraPropsProject'}) DETACH DELETE n"
        )


# ===========================================================================
# RecallSkill
# ===========================================================================


class TestRecallSkill:
    """Integration tests for the recall skill."""

    def test_recall_finds_seeded_node(
        self, engine: EngramaEngine, neo4j_session, seed_recall_data
    ) -> None:
        """Recall finds a node by fulltext search and expands neighbours."""
        skill = RecallSkill()
        results = skill.run(engine, query="P4_ProjectAlpha", limit=5, hops=1)

        assert len(results) >= 1
        hit = results[0]
        assert hit.name == "P4_ProjectAlpha"
        assert hit.label == "Project"
        assert hit.score > 0

        # Should have neighbours (Technology, Problem at 1 hop)
        neighbour_names = {n["name"] for n in hit.neighbours}
        assert "P4_FastAPI" in neighbour_names or \
               "P4_MemoryLeak in worker pool" in neighbour_names

    def test_recall_expands_two_hops(
        self, engine: EngramaEngine, neo4j_session, seed_recall_data
    ) -> None:
        """Recall with hops=2 reaches nodes two relationships away."""
        skill = RecallSkill()
        results = skill.run(engine, query="P4_ProjectAlpha", limit=5, hops=2)

        assert len(results) >= 1
        hit = results[0]
        neighbour_names = {n["name"] for n in hit.neighbours}
        # P4_Caching is 2 hops away: Project -> Problem -> Concept
        assert "P4_Caching" in neighbour_names

    def test_recall_no_results(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Recall returns empty list for unmatched query."""
        skill = RecallSkill()
        results = skill.run(
            engine, query="P4_ZzNonexistentXyZ_12345", limit=5
        )
        assert results == []


# ===========================================================================
# AssociateSkill
# ===========================================================================


class TestAssociateSkill:
    """Integration tests for the associate skill."""

    def test_associate_creates_relationship(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Associate creates a relationship between two existing nodes."""
        # Seed nodes
        neo4j_session.run(
            "MERGE (p:Project {name: $proj}) SET p.test = true, "
            "p.created_at = datetime(), p.updated_at = datetime() "
            "MERGE (t:Technology {name: $tech}) SET t.test = true, "
            "t.created_at = datetime(), t.updated_at = datetime()",
            {"proj": "P4_AssocProject", "tech": "P4_AssocTech"},
        )

        skill = AssociateSkill()
        result = skill.run(
            engine,
            from_name="P4_AssocProject",
            from_label="Project",
            rel_type="USES",
            to_name="P4_AssocTech",
            to_label="Technology",
        )
        assert result["matched"] is True
        assert result["rel_type"] == "USES"

        # Verify relationship exists
        rec = neo4j_session.run(
            "MATCH (p:Project {name: $proj})-[r:USES]->(t:Technology {name: $tech}) "
            "RETURN type(r) AS rel",
            {"proj": "P4_AssocProject", "tech": "P4_AssocTech"},
        ).single()
        assert rec is not None
        assert rec["rel"] == "USES"

    def test_associate_missing_endpoint(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Associate returns matched=False when an endpoint doesn't exist."""
        skill = AssociateSkill()
        result = skill.run(
            engine,
            from_name="P4_NonexistentProject_XYZ",
            from_label="Project",
            rel_type="USES",
            to_name="P4_NonexistentTech_XYZ",
            to_label="Technology",
        )
        assert result["matched"] is False

    def test_associate_invalid_label_raises(self, engine: EngramaEngine) -> None:
        """Associate raises ValueError for unknown labels."""
        skill = AssociateSkill()
        with pytest.raises(ValueError, match="Unknown source label"):
            skill.run(
                engine,
                from_name="x", from_label="FakeLabel",
                rel_type="USES",
                to_name="y", to_label="Technology",
            )

    def test_associate_invalid_rel_raises(self, engine: EngramaEngine) -> None:
        """Associate raises ValueError for unknown relationship types."""
        skill = AssociateSkill()
        with pytest.raises(ValueError, match="Unknown relationship type"):
            skill.run(
                engine,
                from_name="x", from_label="Project",
                rel_type="FAKE_REL",
                to_name="y", to_label="Technology",
            )

    def test_associate_title_keyed_nodes(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Associate works correctly with title-keyed nodes (Decision)."""
        neo4j_session.run(
            "MERGE (p:Problem {title: $prob}) SET p.test = true, "
            "p.created_at = datetime(), p.updated_at = datetime() "
            "MERGE (d:Decision {title: $dec}) SET d.test = true, "
            "d.created_at = datetime(), d.updated_at = datetime()",
            {"prob": "P4_AssocProblem", "dec": "P4_AssocDecision"},
        )

        skill = AssociateSkill()
        result = skill.run(
            engine,
            from_name="P4_AssocProblem",
            from_label="Problem",
            rel_type="SOLVED_BY",
            to_name="P4_AssocDecision",
            to_label="Decision",
        )
        assert result["matched"] is True


# ===========================================================================
# ForgetSkill
# ===========================================================================


class TestForgetSkill:
    """Integration tests for the forget skill."""

    def test_forget_by_name_archives(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Forget by name sets status to 'archived'."""
        # Seed
        neo4j_session.run(
            "MERGE (t:Technology {name: $name}) SET t.test = true, "
            "t.status = 'active', t.created_at = datetime(), t.updated_at = datetime()",
            {"name": "P4_ForgetMe"},
        )

        skill = ForgetSkill()
        result = skill.forget_by_name(
            engine, label="Technology", name="P4_ForgetMe"
        )
        assert result["action"] == "archived"
        assert result["matched"] is True

        # Verify status changed
        rec = neo4j_session.run(
            "MATCH (n:Technology {name: $name}) "
            "RETURN n.status AS status, n.archived_at IS NOT NULL AS has_ts",
            {"name": "P4_ForgetMe"},
        ).single()
        assert rec["status"] == "archived"
        assert rec["has_ts"] is True

    def test_forget_by_name_purge(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Forget with purge=True permanently deletes the node."""
        neo4j_session.run(
            "MERGE (t:Technology {name: $name}) SET t.test = true, "
            "t.created_at = datetime(), t.updated_at = datetime()",
            {"name": "P4_PurgeMe"},
        )

        skill = ForgetSkill()
        result = skill.forget_by_name(
            engine, label="Technology", name="P4_PurgeMe", purge=True
        )
        assert result["action"] == "deleted"
        assert result["matched"] is True

        # Verify node is gone
        rec = neo4j_session.run(
            "MATCH (n:Technology {name: 'P4_PurgeMe'}) RETURN n"
        ).single()
        assert rec is None

    def test_forget_by_name_nonexistent(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Forget returns matched=False for a nonexistent node."""
        skill = ForgetSkill()
        result = skill.forget_by_name(
            engine, label="Technology", name="P4_GhostNode_XYZ"
        )
        assert result["matched"] is False

    def test_forget_by_name_title_keyed(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Forget correctly archives title-keyed nodes (Problem)."""
        neo4j_session.run(
            "MERGE (p:Problem {title: $title}) SET p.test = true, "
            "p.status = 'open', p.created_at = datetime(), p.updated_at = datetime()",
            {"title": "P4_ForgetProblem"},
        )

        skill = ForgetSkill()
        result = skill.forget_by_name(
            engine, label="Problem", name="P4_ForgetProblem"
        )
        assert result["matched"] is True

        rec = neo4j_session.run(
            "MATCH (p:Problem {title: 'P4_ForgetProblem'}) RETURN p.status AS status"
        ).single()
        assert rec["status"] == "archived"

    def test_forget_by_ttl_archives_old_nodes(
        self, engine: EngramaEngine, neo4j_session, seed_old_node
    ) -> None:
        """Forget by TTL archives nodes older than threshold."""
        skill = ForgetSkill()
        result = skill.forget_by_ttl(
            engine, label="Technology", days=365
        )
        assert result["action"] == "archived"
        assert result["count"] >= 1

        # Verify
        rec = neo4j_session.run(
            "MATCH (t:Technology {name: 'P4_ObsoleteTech'}) RETURN t.status AS status"
        ).single()
        assert rec is not None
        assert rec["status"] == "archived"

    def test_forget_by_ttl_purge(
        self, engine: EngramaEngine, neo4j_session, seed_old_node_purge
    ) -> None:
        """Forget by TTL with purge permanently deletes old nodes."""
        skill = ForgetSkill()
        result = skill.forget_by_ttl(
            engine, label="Technology", days=365, purge=True
        )
        assert result["action"] == "deleted"
        assert result["count"] >= 1

        rec = neo4j_session.run(
            "MATCH (t:Technology {name: 'P4_ObsoleteTechPurge'}) RETURN t"
        ).single()
        assert rec is None

    def test_forget_by_ttl_invalid_days(self, engine: EngramaEngine) -> None:
        """Forget by TTL raises ValueError for days < 1."""
        skill = ForgetSkill()
        with pytest.raises(ValueError, match="days must be >= 1"):
            skill.forget_by_ttl(engine, label="Technology", days=0)
