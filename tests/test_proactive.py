"""
Tests for Engrama Phase 6 — proactive skill (surface, approve, dismiss,
write to vault).

Integration tests against a real Neo4j instance + tmp_path vault.
"""

from __future__ import annotations

import pytest

from engrama.adapters.obsidian import ObsidianAdapter
from engrama.core.client import EngramaClient
from engrama.core.engine import EngramaEngine
from engrama.skills.proactive import ProactiveSkill

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> EngramaEngine:
    client = EngramaClient()
    eng = EngramaEngine(client)
    yield eng
    client.close()


@pytest.fixture()
def seed_pending_insights(neo4j_session) -> None:
    """Seed two pending Insights for surface/approve/dismiss tests."""
    neo4j_session.run(
        "MERGE (i:Insight {title: $t1}) "
        "SET i.test = true, i.body = $b1, i.confidence = 0.8, "
        "    i.status = 'pending', i.source_query = 'cross_project_solution', "
        "    i.created_at = datetime(), i.updated_at = datetime() "
        "MERGE (j:Insight {title: $t2}) "
        "SET j.test = true, j.body = $b2, j.confidence = 0.7, "
        "    j.status = 'pending', j.source_query = 'shared_technology', "
        "    j.created_at = datetime() - duration({hours: 1}), j.updated_at = datetime()",
        {
            "t1": "P6_TestInsight_CrossProject",
            "b1": "Solution from project A may apply to project B.",
            "t2": "P6_TestInsight_SharedTech",
            "b2": "Both projects use the same technology.",
        },
    )


@pytest.fixture()
def seed_approved_insight(neo4j_session) -> None:
    """Seed one approved Insight for write-to-vault tests."""
    neo4j_session.run(
        "MERGE (i:Insight {title: $title}) "
        "SET i.test = true, i.body = $body, i.confidence = 0.85, "
        "    i.status = 'approved', i.source_query = 'training_opportunity', "
        "    i.approved_at = datetime(), "
        "    i.created_at = datetime(), i.updated_at = datetime()",
        {
            "title": "P6_ApprovedInsight_Training",
            "body": "The open problem relates to a concept taught in the course.",
        },
    )


@pytest.fixture()
def tmp_vault(tmp_path):
    """Create a minimal vault with one note for write tests."""
    inbox = tmp_path / "00-inbox"
    inbox.mkdir()
    note = inbox / "test-project.md"
    note.write_text(
        "---\ntags: [project]\n---\n\n# Test Project\n\nSome existing content.\n",
        encoding="utf-8",
    )
    return ObsidianAdapter(vault_path=tmp_path)


# ===========================================================================
# Surface
# ===========================================================================


class TestSurface:
    """Tests for ProactiveSkill.surface()."""

    def test_surface_returns_pending_insights(
        self, engine: EngramaEngine, neo4j_session, seed_pending_insights
    ) -> None:
        """Surface returns pending Insights ordered by created_at DESC.

        Note: filter to ``P6_`` prefix so the assertion survives when the
        graph already contains unrelated pending insights from prior
        reflect runs that would otherwise displace the backdated fixture
        from the default limit.
        """
        skill = ProactiveSkill()
        results = skill.surface(engine, limit=50)

        titles = [r.title for r in results if r.title.startswith("P6_")]
        # Our two test insights should be in the list
        assert "P6_TestInsight_CrossProject" in titles
        assert "P6_TestInsight_SharedTech" in titles

        # The newest (CrossProject) should come before the older (SharedTech)
        idx_cross = titles.index("P6_TestInsight_CrossProject")
        idx_shared = titles.index("P6_TestInsight_SharedTech")
        assert idx_cross < idx_shared

    def test_surface_respects_limit(
        self, engine: EngramaEngine, neo4j_session, seed_pending_insights
    ) -> None:
        """Surface with limit=1 returns at most 1 Insight."""
        skill = ProactiveSkill()
        results = skill.surface(engine, limit=1)
        assert len(results) <= 1

    def test_surface_fields(
        self, engine: EngramaEngine, neo4j_session, seed_pending_insights
    ) -> None:
        """Surface returns all expected fields on each Insight."""
        skill = ProactiveSkill()
        results = skill.surface(engine, limit=10)

        test_results = [r for r in results if r.title.startswith("P6_")]
        assert len(test_results) >= 1
        r = test_results[0]
        assert r.body
        assert r.confidence > 0
        assert r.source_query


# ===========================================================================
# Approve / Dismiss
# ===========================================================================


class TestApproveAndDismiss:
    """Tests for ProactiveSkill.approve() and dismiss()."""

    def test_approve_sets_status(
        self, engine: EngramaEngine, neo4j_session, seed_pending_insights
    ) -> None:
        """Approve sets status to 'approved' and records approved_at."""
        skill = ProactiveSkill()
        result = skill.approve(engine, title="P6_TestInsight_CrossProject")
        assert result["matched"] is True
        assert result["action"] == "approved"

        rec = neo4j_session.run(
            "MATCH (i:Insight {title: $t}) "
            "RETURN i.status AS status, i.approved_at IS NOT NULL AS has_ts",
            {"t": "P6_TestInsight_CrossProject"},
        ).single()
        assert rec["status"] == "approved"
        assert rec["has_ts"] is True

    def test_dismiss_sets_status(
        self, engine: EngramaEngine, neo4j_session, seed_pending_insights
    ) -> None:
        """Dismiss sets status to 'dismissed' and records dismissed_at."""
        skill = ProactiveSkill()
        result = skill.dismiss(engine, title="P6_TestInsight_SharedTech")
        assert result["matched"] is True
        assert result["action"] == "dismissed"

        rec = neo4j_session.run(
            "MATCH (i:Insight {title: $t}) "
            "RETURN i.status AS status, i.dismissed_at IS NOT NULL AS has_ts",
            {"t": "P6_TestInsight_SharedTech"},
        ).single()
        assert rec["status"] == "dismissed"
        assert rec["has_ts"] is True

    def test_approve_nonexistent_returns_no_match(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Approve on a nonexistent Insight returns matched=False."""
        skill = ProactiveSkill()
        result = skill.approve(engine, title="P6_GhostInsight_XYZ")
        assert result["matched"] is False

    def test_dismiss_nonexistent_returns_no_match(
        self, engine: EngramaEngine, neo4j_session
    ) -> None:
        """Dismiss on a nonexistent Insight returns matched=False."""
        skill = ProactiveSkill()
        result = skill.dismiss(engine, title="P6_GhostInsight_XYZ")
        assert result["matched"] is False

    def test_surface_excludes_approved(
        self, engine: EngramaEngine, neo4j_session, seed_pending_insights
    ) -> None:
        """After approval, the Insight no longer appears in surface()."""
        skill = ProactiveSkill()
        skill.approve(engine, title="P6_TestInsight_CrossProject")

        results = skill.surface(engine, limit=50)
        titles = [r.title for r in results]
        assert "P6_TestInsight_CrossProject" not in titles


# ===========================================================================
# Write to Vault
# ===========================================================================


class TestWriteToVault:
    """Tests for ProactiveSkill.write_to_vault()."""

    def test_write_approved_insight(
        self, engine: EngramaEngine, neo4j_session, seed_approved_insight, tmp_vault
    ) -> None:
        """Write appends approved Insight as markdown section."""
        skill = ProactiveSkill()
        result = skill.write_to_vault(
            engine,
            tmp_vault,
            title="P6_ApprovedInsight_Training",
            target_note="00-inbox/test-project.md",
        )
        assert result["written"] is True

        # Verify note content
        note = tmp_vault.read_note("00-inbox/test-project.md")
        assert "## Insight: P6_ApprovedInsight_Training" in note["content"]
        assert "training_opportunity" in note["content"]
        assert "85%" in note["content"]

        # Verify synced_at in Neo4j
        rec = neo4j_session.run(
            "MATCH (i:Insight {title: $t}) "
            "RETURN i.obsidian_path AS path, i.synced_at IS NOT NULL AS synced",
            {"t": "P6_ApprovedInsight_Training"},
        ).single()
        assert rec["synced"] is True
        assert rec["path"] == "00-inbox/test-project.md"

    def test_write_rejects_pending_insight(
        self, engine: EngramaEngine, neo4j_session, seed_pending_insights, tmp_vault
    ) -> None:
        """Write refuses to write a pending (unapproved) Insight."""
        skill = ProactiveSkill()
        result = skill.write_to_vault(
            engine,
            tmp_vault,
            title="P6_TestInsight_CrossProject",
            target_note="00-inbox/test-project.md",
        )
        assert result["written"] is False
        assert "not 'approved'" in result["reason"]

    def test_write_nonexistent_insight(
        self, engine: EngramaEngine, neo4j_session, tmp_vault
    ) -> None:
        """Write returns written=False for nonexistent Insight."""
        skill = ProactiveSkill()
        result = skill.write_to_vault(
            engine,
            tmp_vault,
            title="P6_GhostInsight_XYZ",
            target_note="00-inbox/test-project.md",
        )
        assert result["written"] is False
        assert "not found" in result["reason"]

    def test_write_nonexistent_note(
        self, engine: EngramaEngine, neo4j_session, seed_approved_insight, tmp_vault
    ) -> None:
        """Write returns written=False for nonexistent target note."""
        skill = ProactiveSkill()
        result = skill.write_to_vault(
            engine,
            tmp_vault,
            title="P6_ApprovedInsight_Training",
            target_note="00-inbox/no-such-note.md",
        )
        assert result["written"] is False
        assert "not found" in result["reason"]
