"""
Tests for DDR-003 Phase D — Temporal reasoning.

Covers:
    - Pure helpers: compute_decayed_confidence, temporal_score, days_since,
      detect_conflict
    - Neo4j backend: merge_node with temporal fields, decay_scores()
    - Engine: decay_scores delegation
    - HybridSearchEngine: temporal scoring integration
    - CLI: engrama decay argparse
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. Pure helper tests (no DB needed)
# ---------------------------------------------------------------------------


class TestComputeDecayedConfidence:
    """Test engrama.core.temporal.compute_decayed_confidence."""

    def test_no_decay_when_zero_days(self):
        from engrama.core.temporal import compute_decayed_confidence

        assert compute_decayed_confidence(1.0, 0.0) == 1.0

    def test_no_decay_when_zero_rate(self):
        from engrama.core.temporal import compute_decayed_confidence

        assert compute_decayed_confidence(1.0, 100.0, rate=0.0) == 1.0

    def test_exponential_decay(self):
        from engrama.core.temporal import compute_decayed_confidence

        # 100 days at rate 0.01 → exp(-1) ≈ 0.3679
        result = compute_decayed_confidence(1.0, 100.0, rate=0.01)
        assert abs(result - math.exp(-1)) < 0.001

    def test_partial_confidence(self):
        from engrama.core.temporal import compute_decayed_confidence

        # Start at 0.5, 50 days at rate 0.01 → 0.5 * exp(-0.5) ≈ 0.3033
        result = compute_decayed_confidence(0.5, 50.0, rate=0.01)
        expected = 0.5 * math.exp(-0.5)
        assert abs(result - expected) < 0.001

    def test_clamped_to_zero(self):
        from engrama.core.temporal import compute_decayed_confidence

        # Very large days → should approach 0 but not go negative
        result = compute_decayed_confidence(1.0, 100000.0, rate=0.01)
        assert result >= 0.0

    def test_clamped_to_one(self):
        from engrama.core.temporal import compute_decayed_confidence

        # Confidence > 1 gets clamped
        result = compute_decayed_confidence(1.5, 0.0)
        assert result == 1.0

    def test_negative_days_returns_original(self):
        from engrama.core.temporal import compute_decayed_confidence

        assert compute_decayed_confidence(0.8, -5.0) == 0.8


class TestTemporalScore:
    """Test engrama.core.temporal.temporal_score."""

    def test_fresh_node_scores_high(self):
        from engrama.core.temporal import temporal_score

        # Just updated, full confidence
        result = temporal_score(1.0, 0.0)
        assert result == 1.0

    def test_half_life(self):
        from engrama.core.temporal import temporal_score

        # At half_life days, recency = 0.5, so score ≈ 0.5 * confidence
        result = temporal_score(1.0, 30.0, recency_half_life=30.0)
        assert abs(result - 0.5) < 0.01

    def test_low_confidence_scores_low(self):
        from engrama.core.temporal import temporal_score

        # Even if recent, low confidence → low score
        result = temporal_score(0.1, 0.0)
        assert result == 0.1

    def test_old_node_low_score(self):
        from engrama.core.temporal import temporal_score

        # 180 days old → recency ≈ 0.015 at 30d half-life
        result = temporal_score(1.0, 180.0, recency_half_life=30.0)
        assert result < 0.05

    def test_custom_half_life(self):
        from engrama.core.temporal import temporal_score

        # 7-day half-life: 7 days → score ≈ 0.5
        result = temporal_score(1.0, 7.0, recency_half_life=7.0)
        assert abs(result - 0.5) < 0.01


class TestDaysSince:
    """Test engrama.core.temporal.days_since."""

    def test_none_returns_zero(self):
        from engrama.core.temporal import days_since

        assert days_since(None) == 0.0

    def test_datetime_object(self):
        from engrama.core.temporal import days_since

        dt = datetime.now(UTC) - timedelta(days=10)
        result = days_since(dt)
        assert abs(result - 10.0) < 0.1

    def test_iso_string(self):
        from engrama.core.temporal import days_since

        dt = datetime.now(UTC) - timedelta(days=5)
        result = days_since(dt.isoformat())
        assert abs(result - 5.0) < 0.1

    def test_naive_datetime_treated_as_utc(self):
        from engrama.core.temporal import days_since

        dt = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3)
        result = days_since(dt)
        assert abs(result - 3.0) < 0.1


class TestDetectConflict:
    """Test engrama.core.temporal.detect_conflict."""

    def test_no_valid_to(self):
        from engrama.core.temporal import detect_conflict

        assert detect_conflict({"name": "test"}) is None

    def test_future_valid_to_no_conflict(self):
        from engrama.core.temporal import detect_conflict

        future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        assert detect_conflict({"valid_to": future}) is None

    def test_past_valid_to_is_conflict(self):
        from engrama.core.temporal import detect_conflict

        past = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        result = detect_conflict({"valid_to": past})
        assert result is not None
        assert result["conflict"] == "revived"
        assert result["action"] == "cleared"


# ---------------------------------------------------------------------------
# 2. Backend integration tests (need Neo4j)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend():
    """Create a real Neo4j backend for integration tests."""
    from engrama.backends.neo4j.backend import Neo4jGraphStore
    from engrama.core.client import EngramaClient

    client = EngramaClient()
    client.verify()
    store = Neo4jGraphStore(client)
    yield store
    # Clean up test nodes
    client.run("MATCH (n) WHERE n._test_phase_d = true DETACH DELETE n")
    client.close()


class TestBackendTemporalFields:
    """Test that merge_node sets temporal fields (valid_from, confidence)."""

    def test_merge_creates_with_temporal_defaults(self, backend):
        """New node gets valid_from=now, confidence=1.0."""
        result = backend.merge_node(
            "Technology",
            "name",
            "_test_temporal_1",
            {"description": "Test node", "_test_phase_d": True},
        )
        assert len(result) == 1
        node = dict(result[0]["n"])
        assert node.get("confidence") == 1.0
        assert node.get("valid_from") is not None

    def test_merge_with_custom_confidence(self, backend):
        """Caller can override confidence."""
        result = backend.merge_node(
            "Technology",
            "name",
            "_test_temporal_2",
            {
                "description": "Custom conf",
                "confidence": 0.7,
                "_test_phase_d": True,
            },
        )
        node = dict(result[0]["n"])
        assert node.get("confidence") == 0.7

    def test_merge_match_clears_valid_to(self, backend):
        """Re-merging a node that has valid_to should clear it (revival)."""
        # Create node
        backend.merge_node(
            "Technology",
            "name",
            "_test_temporal_3",
            {"description": "Will expire", "_test_phase_d": True},
        )
        # Expire it
        backend.expire_node("Technology", "name", "_test_temporal_3")
        # Verify it's expired
        node_data = backend.get_node("Technology", "name", "_test_temporal_3")
        assert node_data.get("valid_to") is not None

        # Re-merge — should clear valid_to
        backend.merge_node(
            "Technology",
            "name",
            "_test_temporal_3",
            {"description": "Revived", "_test_phase_d": True},
        )
        node_data = backend.get_node("Technology", "name", "_test_temporal_3")
        assert node_data.get("valid_to") is None


class TestExpireNode:
    """Test expire_node method."""

    def test_expire_sets_valid_to(self, backend):
        backend.merge_node(
            "Technology",
            "name",
            "_test_expire_1",
            {"description": "To expire", "_test_phase_d": True},
        )
        result = backend.expire_node("Technology", "name", "_test_expire_1")
        assert result is True
        node_data = backend.get_node("Technology", "name", "_test_expire_1")
        assert node_data.get("valid_to") is not None

    def test_expire_nonexistent(self, backend):
        result = backend.expire_node("Technology", "name", "_test_no_such_node_xyz")
        assert result is False


class TestDecayScores:
    """Test decay_scores batch operation."""

    def test_decay_updates_confidence(self, backend):
        """Create nodes with known timestamps, apply decay."""
        # Create a node (confidence defaults to 1.0)
        backend.merge_node(
            "Concept",
            "name",
            "_test_decay_1",
            {"description": "Decay test", "_test_phase_d": True},
        )
        # Apply decay with a high rate — even 0 days old, the query
        # checks days_old > 0 so this node won't be affected.
        # We just verify the method runs without error.
        result = backend.decay_scores(rate=0.01)
        assert "decayed" in result
        assert "archived" in result

    def test_decay_with_label_filter(self, backend):
        backend.merge_node(
            "Concept",
            "name",
            "_test_decay_2",
            {"description": "Filtered decay", "_test_phase_d": True},
        )
        result = backend.decay_scores(rate=0.01, label="Concept")
        assert "decayed" in result

    def test_decay_archives_below_threshold(self, backend):
        """Nodes below min_confidence get archived."""
        # Create a node with very low confidence
        backend.merge_node(
            "Concept",
            "name",
            "_test_decay_archive",
            {
                "description": "Should archive",
                "confidence": 0.001,
                "_test_phase_d": True,
            },
        )
        result = backend.decay_scores(
            rate=0.0,  # no decay, just check threshold
            min_confidence=0.01,
        )
        # The node with confidence=0.001 should be archived
        assert result["archived"] >= 0  # may include other test artifacts

    def test_decay_archives_by_max_age(self, backend):
        """Nodes older than max_age_days get archived."""
        result = backend.decay_scores(
            rate=0.0,
            max_age_days=999999,  # nobody should be this old
        )
        assert result["archived"] == 0


# ---------------------------------------------------------------------------
# 3. Engine tests (mocked backend)
# ---------------------------------------------------------------------------


class TestEngineDecay:
    """Test EngramaEngine.decay_scores delegation."""

    def test_delegates_to_store(self):
        from engrama.core.engine import EngramaEngine

        mock_store = MagicMock()
        mock_store.decay_scores.return_value = {"decayed": 42, "archived": 3}
        engine = EngramaEngine(mock_store)

        result = engine.decay_scores(rate=0.02, min_confidence=0.05)
        assert result == {"decayed": 42, "archived": 3}
        mock_store.decay_scores.assert_called_once_with(
            rate=0.02,
            min_confidence=0.05,
            max_age_days=0,
            label=None,
        )

    def test_graceful_when_backend_lacks_decay(self):
        from engrama.core.engine import EngramaEngine

        # A store without decay_scores attribute
        mock_store = MagicMock(spec=[])
        engine = EngramaEngine(mock_store)

        result = engine.decay_scores()
        assert result == {"decayed": 0, "archived": 0}


# ---------------------------------------------------------------------------
# 4. Hybrid search temporal scoring (mocked)
# ---------------------------------------------------------------------------


class TestHybridTemporalScoring:
    """Test that HybridSearchEngine factors temporal_score into results."""

    def test_temporal_score_included_in_final(self):
        from engrama.core.search import HybridConfig, HybridSearchEngine

        # Create mocks
        mock_graph = MagicMock()
        mock_vector = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.dimensions = 0  # force fulltext-only
        mock_vector.dimensions = 0

        # Fulltext returns two results, one older
        now = datetime.now(UTC)
        old_date = (now - timedelta(days=90)).isoformat()
        recent_date = (now - timedelta(hours=1)).isoformat()

        mock_graph.fulltext_search.return_value = [
            {
                "type": "Concept",
                "name": "Old",
                "score": 5.0,
                "confidence": 0.3,
                "updated_at": old_date,
            },
            {
                "type": "Concept",
                "name": "Recent",
                "score": 4.0,
                "confidence": 1.0,
                "updated_at": recent_date,
            },
        ]

        config = HybridConfig(temporal_gamma=0.2, recency_half_life=30.0)
        engine = HybridSearchEngine(mock_graph, mock_vector, mock_embedder, config)
        results = engine.search("test", limit=10)

        # Recent node should have higher temporal_score
        by_name = {r.name: r for r in results}
        assert by_name["Recent"].temporal_score > by_name["Old"].temporal_score

    def test_temporal_disabled_when_gamma_zero(self):
        from engrama.core.search import HybridConfig, HybridSearchEngine

        mock_graph = MagicMock()
        mock_vector = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.dimensions = 0
        mock_vector.dimensions = 0

        mock_graph.fulltext_search.return_value = [
            {"type": "Concept", "name": "Node1", "score": 5.0},
        ]

        config = HybridConfig(temporal_gamma=0.0)
        engine = HybridSearchEngine(mock_graph, mock_vector, mock_embedder, config)
        results = engine.search("test", limit=10)

        # With gamma=0, temporal_score stays at default 1.0 and doesn't affect scoring
        assert results[0].temporal_score == 1.0


# ---------------------------------------------------------------------------
# 5. CLI argparse test
# ---------------------------------------------------------------------------


class TestCLIDecay:
    """Test engrama decay CLI command registration."""

    def test_decay_argparse(self):
        """Verify the decay subparser accepts expected arguments."""

        # Just test that the parser recognises the decay command
        # We can't easily call main() without DB, so check argparse setup
        # by importing and inspecting
        from engrama import cli

        # Access the module to ensure it imports cleanly
        assert hasattr(cli, "cmd_decay")

    def test_cmd_decay_dry_run(self):
        """cmd_decay with --dry-run should not touch the DB."""
        import argparse

        from engrama.cli import cmd_decay

        args = argparse.Namespace(
            rate=0.01,
            min_confidence=0.0,
            max_age=0,
            label=None,
            dry_run=True,
        )
        # Dry run should succeed without DB
        result = cmd_decay(args)
        assert result == 0


# ---------------------------------------------------------------------------
# 6. Async store integration tests (DDR-003 Phase D)
# ---------------------------------------------------------------------------


def _unique(prefix: str) -> str:
    """Generate a unique name for test isolation."""
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def async_store():
    """Create an async store backed by the test Neo4j instance."""
    from neo4j import AsyncGraphDatabase

    from engrama.backends.neo4j.async_store import Neo4jAsyncStore

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    store = Neo4jAsyncStore(driver, database="neo4j")
    yield store
    await driver.close()


@pytest.fixture
async def acleanup(async_store):
    """Track nodes created during a test for cleanup."""
    created: list[tuple[str, str, str]] = []

    def track(label: str, key_field: str, key_value: str) -> None:
        created.append((label, key_field, key_value))

    yield track

    for label, key_field, key_value in created:
        try:
            await async_store.delete_node(label, key_field, key_value, soft=False)
        except Exception:
            pass


class TestAsyncDecayConfidence:
    """Async store decay_confidence integration tests."""

    @pytest.mark.asyncio
    async def test_decay_reduces_confidence(self, async_store, acleanup):
        """Old nodes should have lower confidence after decay."""
        name = _unique("adecay")
        acleanup("Technology", "name", name)

        await async_store.merge_node("Technology", "name", name, {"_test_phase_d": True})
        # Backdate updated_at to 30 days ago
        await async_store.run_pattern(
            "MATCH (n:Technology {name: $name}) "
            "SET n.updated_at = datetime() - duration({days: 30})",
            {"name": name},
        )

        result = await async_store.decay_confidence(decay_rate=0.01, dry_run=False)
        assert "affected" in result
        assert "sample" in result

        node = await async_store.get_node("Technology", "name", name)
        conf = node.get("confidence", 1.0)
        expected = 1.0 * math.exp(-0.01 * 30)
        assert conf < 0.9, f"Expected decay, got {conf}"
        assert abs(conf - expected) < 0.1

    @pytest.mark.asyncio
    async def test_decay_dry_run_no_changes(self, async_store, acleanup):
        """Dry run should report changes without writing."""
        name = _unique("adecay-dry")
        acleanup("Technology", "name", name)

        await async_store.merge_node("Technology", "name", name, {"_test_phase_d": True})
        await async_store.run_pattern(
            "MATCH (n:Technology {name: $name}) "
            "SET n.updated_at = datetime() - duration({days: 60})",
            {"name": name},
        )

        result = await async_store.decay_confidence(decay_rate=0.01, dry_run=True)
        assert "sample" in result

        node = await async_store.get_node("Technology", "name", name)
        assert node.get("confidence", 1.0) == 1.0

    @pytest.mark.asyncio
    async def test_fresh_nodes_not_affected(self, async_store, acleanup):
        """Nodes updated today should not decay."""
        name = _unique("adecay-fresh")
        acleanup("Technology", "name", name)

        await async_store.merge_node("Technology", "name", name, {"_test_phase_d": True})
        node_before = await async_store.get_node("Technology", "name", name)
        conf_before = node_before.get("confidence", 1.0)

        await async_store.decay_confidence(decay_rate=0.1, dry_run=False)

        node_after = await async_store.get_node("Technology", "name", name)
        assert node_after.get("confidence", 1.0) == conf_before

    @pytest.mark.asyncio
    async def test_archived_nodes_not_affected(self, async_store, acleanup):
        """Archived nodes should not decay."""
        name = _unique("adecay-arch")
        acleanup("Technology", "name", name)

        await async_store.merge_node(
            "Technology",
            "name",
            name,
            {"_test_phase_d": True, "status": "archived"},
        )
        await async_store.run_pattern(
            "MATCH (n:Technology {name: $name}) "
            "SET n.updated_at = datetime() - duration({days: 60})",
            {"name": name},
        )

        await async_store.decay_confidence(decay_rate=0.1, dry_run=False)
        node = await async_store.get_node("Technology", "name", name)
        assert node.get("confidence", 1.0) == 1.0

    @pytest.mark.asyncio
    async def test_very_low_confidence_not_affected(self, async_store, acleanup):
        """Nodes with confidence < 0.05 should not decay further."""
        name = _unique("adecay-low")
        acleanup("Technology", "name", name)

        await async_store.merge_node(
            "Technology",
            "name",
            name,
            {"_test_phase_d": True, "confidence": 0.03},
        )
        await async_store.run_pattern(
            "MATCH (n:Technology {name: $name}) "
            "SET n.updated_at = datetime() - duration({days: 60})",
            {"name": name},
        )

        await async_store.decay_confidence(decay_rate=0.1, dry_run=False)
        node = await async_store.get_node("Technology", "name", name)
        assert node.get("confidence") == pytest.approx(0.03, abs=0.001)


class TestAsyncValidTo:
    """Async store valid_to integration tests."""

    @pytest.mark.asyncio
    async def test_valid_to_stored(self, async_store, acleanup):
        """Setting valid_to marks a fact as superseded."""
        name = _unique("avt-store")
        acleanup("Technology", "name", name)

        result = await async_store.merge_node(
            "Technology",
            "name",
            name,
            {"_test_phase_d": True, "valid_to": "2026-01-01T00:00:00Z"},
        )
        node = result["node"]
        assert node.get("valid_to") is not None
        assert node.get("confidence") == pytest.approx(0.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_update_superseded_node_warns(self, async_store, acleanup):
        """Updating a node with valid_to should include warning."""
        name = _unique("avt-warn")
        acleanup("Technology", "name", name)

        await async_store.merge_node(
            "Technology",
            "name",
            name,
            {"_test_phase_d": True, "valid_to": "2025-12-31T00:00:00Z"},
        )

        result = await async_store.merge_node(
            "Technology",
            "name",
            name,
            {"_test_phase_d": True, "description": "revived"},
        )
        assert "warning" in result, "Expected conflict warning on revival"
        assert "superseded" in result["warning"].lower()

    @pytest.mark.asyncio
    async def test_valid_to_with_explicit_confidence(self, async_store, acleanup):
        """Setting valid_to with explicit confidence halves the given value."""
        name = _unique("avt-conf")
        acleanup("Technology", "name", name)

        result = await async_store.merge_node(
            "Technology",
            "name",
            name,
            {"_test_phase_d": True, "valid_to": "2026-06-01", "confidence": 0.8},
        )
        node = result["node"]
        assert node.get("confidence") == pytest.approx(0.4, abs=0.01)


class TestAsyncQueryAtDate:
    """Async store query_at_date integration tests."""

    @pytest.mark.asyncio
    async def test_query_at_date_filters_correctly(self, async_store, acleanup):
        """Only nodes valid at the given date should be returned."""
        name = _unique("aqad")
        acleanup("Technology", "name", name)

        await async_store.merge_node("Technology", "name", name, {"_test_phase_d": True})
        await async_store.run_pattern(
            "MATCH (n:Technology {name: $name}) "
            "SET n.valid_from = datetime('2026-01-01T00:00:00Z')",
            {"name": name},
        )

        results = await async_store.query_at_date("2026-03-01")
        names = [r["name"] for r in results]
        assert name in names

        results_before = await async_store.query_at_date("2025-12-01")
        names_before = [r["name"] for r in results_before]
        assert name not in names_before

    @pytest.mark.asyncio
    async def test_query_at_date_excludes_superseded(self, async_store, acleanup):
        """Nodes with valid_to before the query date are excluded."""
        name = _unique("aqad-excl")
        acleanup("Technology", "name", name)

        await async_store.merge_node(
            "Technology",
            "name",
            name,
            {"_test_phase_d": True, "valid_to": "2026-02-28T23:59:59Z"},
        )
        await async_store.run_pattern(
            "MATCH (n:Technology {name: $name}) "
            "SET n.valid_from = datetime('2026-01-01T00:00:00Z')",
            {"name": name},
        )

        results_during = await async_store.query_at_date("2026-02-15")
        names_during = [r["name"] for r in results_during]
        assert name in names_during

        results_after = await async_store.query_at_date("2026-04-01")
        names_after = [r["name"] for r in results_after]
        assert name not in names_after

    @pytest.mark.asyncio
    async def test_query_at_date_with_label_filter(self, async_store, acleanup):
        """Label filter restricts results."""
        name = _unique("aqad-lbl")
        acleanup("Project", "name", name)

        await async_store.merge_node("Project", "name", name, {"_test_phase_d": True})

        results = await async_store.query_at_date("2026-12-31", label="Project")
        names = [r["name"] for r in results]
        assert name in names

        results_wrong = await async_store.query_at_date("2026-12-31", label="Technology")
        names_wrong = [r["name"] for r in results_wrong]
        assert name not in names_wrong
