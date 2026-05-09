"""
Tests for Engrama Phase 7 — Python SDK public API.

Integration tests against a real Neo4j instance — the SDK fixture
explicitly opts in to ``backend="neo4j"`` because these tests assert
about Neo4j-shaped state (via the ``neo4j_session`` fixture).
"""

from __future__ import annotations

import pytest

from engrama import Engrama


@pytest.fixture()
def eng() -> Engrama:
    """SDK pinned to the Neo4j backend (matches neo4j_session writes)."""
    e = Engrama(backend="neo4j")
    yield e
    e.close()


# ===========================================================================
# Connection
# ===========================================================================


class TestConnection:
    """Basic connectivity and lifecycle."""

    def test_context_manager(self) -> None:
        """Engrama works as a context manager."""
        with Engrama(backend="neo4j") as e:
            e.verify()
        # Connection should be closed after __exit__

    def test_repr(self, eng: Engrama) -> None:
        """Repr identifies the backend in use."""
        r = repr(eng)
        assert "Engrama(" in r
        assert "Neo4jGraphStore" in r


# ===========================================================================
# Remember + Search
# ===========================================================================


class TestRememberAndSearch:
    """SDK remember and search integration."""

    def test_remember_and_search(self, eng: Engrama, neo4j_session) -> None:
        """Remember a node and find it via search."""
        eng.remember("Technology", "SDK_TestTech", "A test technology")

        results = eng.search("SDK_TestTech", limit=5)
        names = [r["name"] for r in results]
        assert "SDK_TestTech" in names

        # Cleanup
        neo4j_session.run(
            "MATCH (n:Technology {name: 'SDK_TestTech'}) DETACH DELETE n"
        )

    def test_remember_with_extra_kwargs(self, eng: Engrama, neo4j_session) -> None:
        """Remember passes kwargs as extra properties."""
        eng.remember("Project", "SDK_TestProject", "Test project",
                     status="active", repo="github.com/test")

        rec = neo4j_session.run(
            "MATCH (n:Project {name: $name}) "
            "RETURN n.status AS status, n.repo AS repo",
            {"name": "SDK_TestProject"},
        ).single()
        assert rec["status"] == "active"
        assert rec["repo"] == "github.com/test"

        # Cleanup
        neo4j_session.run(
            "MATCH (n:Project {name: 'SDK_TestProject'}) DETACH DELETE n"
        )


# ===========================================================================
# Recall
# ===========================================================================


class TestRecall:
    """SDK recall integration."""

    def test_recall_with_neighbours(self, eng: Engrama, neo4j_session) -> None:
        """Recall expands seed node with neighbours."""
        # Seed a small graph
        neo4j_session.run(
            "MERGE (p:Project {name: $proj}) "
            "SET p.test = true, p.status = 'active', "
            "    p.created_at = datetime(), p.updated_at = datetime() "
            "MERGE (t:Technology {name: $tech}) "
            "SET t.test = true, t.created_at = datetime(), t.updated_at = datetime() "
            "MERGE (p)-[:USES]->(t)",
            {"proj": "SDK_RecallProject", "tech": "SDK_RecallTech"},
        )

        results = eng.recall("SDK_RecallProject", hops=1)
        assert len(results) >= 1
        hit = results[0]
        assert hit.name == "SDK_RecallProject"
        neighbour_names = {n["name"] for n in hit.neighbours}
        assert "SDK_RecallTech" in neighbour_names

    def test_recall_no_results(self, eng: Engrama) -> None:
        """Recall returns empty for non-matching query."""
        results = eng.recall("SDK_Nonexistent_ZZZ_12345")
        assert results == []


# ===========================================================================
# Associate
# ===========================================================================


class TestAssociate:
    """SDK associate integration."""

    def test_associate_creates_rel(self, eng: Engrama, neo4j_session) -> None:
        """Associate creates a relationship between existing nodes."""
        neo4j_session.run(
            "MERGE (p:Project {name: $proj}) SET p.test = true, "
            "p.created_at = datetime(), p.updated_at = datetime() "
            "MERGE (t:Technology {name: $tech}) SET t.test = true, "
            "t.created_at = datetime(), t.updated_at = datetime()",
            {"proj": "SDK_AssocProj", "tech": "SDK_AssocTech"},
        )

        result = eng.associate(
            "SDK_AssocProj", "Project", "USES", "SDK_AssocTech", "Technology"
        )
        assert result["matched"] is True

    def test_associate_validation(self, eng: Engrama) -> None:
        """Associate raises ValueError for invalid labels."""
        with pytest.raises(ValueError, match="Unknown"):
            eng.associate("x", "FakeLabel", "USES", "y", "Technology")


# ===========================================================================
# Forget
# ===========================================================================


class TestForget:
    """SDK forget integration."""

    def test_forget_archives(self, eng: Engrama, neo4j_session) -> None:
        """Forget archives a node by default."""
        neo4j_session.run(
            "MERGE (t:Technology {name: $name}) SET t.test = true, "
            "t.status = 'active', t.created_at = datetime(), t.updated_at = datetime()",
            {"name": "SDK_ForgetMe"},
        )

        result = eng.forget("Technology", "SDK_ForgetMe")
        assert result["action"] == "archived"
        assert result["matched"] is True

    def test_forget_purge(self, eng: Engrama, neo4j_session) -> None:
        """Forget with purge deletes permanently."""
        neo4j_session.run(
            "MERGE (t:Technology {name: $name}) SET t.test = true, "
            "t.created_at = datetime(), t.updated_at = datetime()",
            {"name": "SDK_PurgeMe"},
        )

        result = eng.forget("Technology", "SDK_PurgeMe", purge=True)
        assert result["action"] == "deleted"


# ===========================================================================
# Reflect + Proactive
# ===========================================================================


class TestReflectAndProactive:
    """SDK reflect and proactive lifecycle."""

    def test_reflect_runs(self, eng: Engrama) -> None:
        """Reflect runs without error and returns a list."""
        insights = eng.reflect()
        assert isinstance(insights, list)

    def test_surface_and_approve_cycle(
        self, eng: Engrama, neo4j_session
    ) -> None:
        """Full lifecycle: seed pending Insight → surface → approve."""
        neo4j_session.run(
            "MERGE (i:Insight {title: $title}) "
            "SET i.test = true, i.body = 'SDK test insight', "
            "    i.confidence = 0.9, i.status = 'pending', "
            "    i.source_query = 'test', "
            "    i.created_at = datetime(), i.updated_at = datetime()",
            {"title": "SDK_TestInsight"},
        )

        pending = eng.surface_insights(limit=50)
        titles = [p.title for p in pending]
        assert "SDK_TestInsight" in titles

        result = eng.approve_insight("SDK_TestInsight")
        assert result["matched"] is True

        # No longer pending
        pending2 = eng.surface_insights(limit=50)
        titles2 = [p.title for p in pending2]
        assert "SDK_TestInsight" not in titles2

    def test_dismiss_insight(self, eng: Engrama, neo4j_session) -> None:
        """Dismiss sets status to dismissed."""
        neo4j_session.run(
            "MERGE (i:Insight {title: $title}) "
            "SET i.test = true, i.body = 'SDK dismiss test', "
            "    i.confidence = 0.5, i.status = 'pending', "
            "    i.source_query = 'test', "
            "    i.created_at = datetime(), i.updated_at = datetime()",
            {"title": "SDK_DismissInsight"},
        )

        result = eng.dismiss_insight("SDK_DismissInsight")
        assert result["matched"] is True

    def test_write_to_vault_requires_obsidian(self, eng: Engrama) -> None:
        """write_insight_to_vault raises if no vault is connected."""
        if eng.has_vault:
            pytest.skip("Vault is connected in this environment")

        with pytest.raises(RuntimeError, match="Obsidian adapter not available"):
            eng.write_insight_to_vault("x", "y.md")
