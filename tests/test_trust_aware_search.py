"""Tests for DDR-003 Phase E layer 3 — trust-aware retrieval.

The hybrid search engine now incorporates the per-node ``trust_level``
property into its final score:

    final = α·vector + (1-α)·fulltext + β·graph_boost
            + γ·temporal + δ·trust_score

These tests use the same mock-store harness as ``test_hybrid_search``.
"""

from __future__ import annotations

from typing import Any

import pytest

from engrama.core.search import (
    DEFAULT_TRUST_SCORE,
    HybridConfig,
    HybridSearchEngine,
    SearchResult,
)

# ---------------------------------------------------------------------------
# Minimal mock stores (kept local so this file doesn't import from another
# test module — pytest discovery shouldn't introduce inter-test coupling).
# ---------------------------------------------------------------------------


class _NullEmbedder:
    dimensions = 0

    def embed(self, text: str) -> list[float]:
        return []


class _MockEmbedder:
    dimensions = 4

    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]


class _MockGraph:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def fulltext_search(self, query: str, limit: int = 10, **kwargs) -> list[dict[str, Any]]:
        return self._rows[:limit]


class _NullVector:
    dimensions = 0

    def search_vectors(self, *a, **kw) -> list[dict[str, Any]]:
        return []


class _MockVector:
    dimensions = 4

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def search_vectors(self, *a, **kw) -> list[dict[str, Any]]:
        return self._rows


# ---------------------------------------------------------------------------
# 1. HybridConfig defaults + env override
# ---------------------------------------------------------------------------


class TestTrustConfig:
    def test_default_trust_delta_matches_ddr(self):
        cfg = HybridConfig()
        assert cfg.trust_delta == 0.1

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ENGRAMA_TRUST_DELTA", "0.25")
        cfg = HybridConfig()
        assert cfg.trust_delta == 0.25

    def test_env_disable(self, monkeypatch):
        monkeypatch.setenv("ENGRAMA_TRUST_DELTA", "0.0")
        cfg = HybridConfig()
        assert cfg.trust_delta == 0.0

    def test_env_invalid_keeps_default(self, monkeypatch, caplog):
        monkeypatch.setenv("ENGRAMA_TRUST_DELTA", "not-a-float")
        cfg = HybridConfig()
        assert cfg.trust_delta == 0.1
        assert "ENGRAMA_TRUST_DELTA" in caplog.text


# ---------------------------------------------------------------------------
# 2. SearchResult default trust_score
# ---------------------------------------------------------------------------


class TestSearchResultDefaults:
    def test_default_trust_score_is_neutral(self):
        r = SearchResult(name="X")
        assert r.trust_score == DEFAULT_TRUST_SCORE == 0.5


# ---------------------------------------------------------------------------
# 3. Ranking honours trust_level (fulltext-only path, deterministic)
# ---------------------------------------------------------------------------


class TestTrustRanking:
    def _engine(self, rows, *, trust_delta=0.1):
        return HybridSearchEngine(
            _MockGraph(rows),
            _NullVector(),
            _NullEmbedder(),
            config=HybridConfig(
                alpha=0.0,
                graph_beta=0.0,
                temporal_gamma=0.0,
                trust_delta=trust_delta,
            ),
        )

    def test_higher_trust_wins_at_equal_relevance(self):
        # Two nodes with identical fulltext score; only trust differs.
        rows = [
            {"type": "Concept", "name": "low", "score": 1.0, "trust_level": 0.3},
            {"type": "Concept", "name": "high", "score": 1.0, "trust_level": 0.9},
        ]
        engine = self._engine(rows)
        results = engine.search("q", limit=5)
        assert [r.name for r in results] == ["high", "low"]
        assert results[0].trust_score == 0.9
        assert results[1].trust_score == 0.3

    def test_trust_delta_zero_disables_trust_signal(self):
        # Same rows, but trust_delta=0 means trust doesn't influence ranking.
        rows = [
            {"type": "Concept", "name": "low", "score": 1.0, "trust_level": 0.3},
            {"type": "Concept", "name": "high", "score": 1.0, "trust_level": 0.9},
        ]
        engine = self._engine(rows, trust_delta=0.0)
        results = engine.search("q", limit=5)
        # Both have equal fulltext_score=1.0 after normalisation; with
        # trust_delta=0 their final scores tie and order is whichever
        # dict insertion order produced. The contract here is that the
        # trust signal does NOT contribute, so final_score should equal
        # the pre-trust formula value (1.0).
        assert all(r.final_score == pytest.approx(1.0) for r in results)

    def test_missing_trust_level_uses_neutral_default(self):
        # One node has trust_level=0.9, the other has none → defaults to 0.5.
        rows = [
            {"type": "Concept", "name": "neutral", "score": 1.0},
            {"type": "Concept", "name": "trusted", "score": 1.0, "trust_level": 0.9},
        ]
        engine = self._engine(rows)
        results = engine.search("q", limit=5)
        neutral = next(r for r in results if r.name == "neutral")
        trusted = next(r for r in results if r.name == "trusted")
        assert neutral.trust_score == DEFAULT_TRUST_SCORE
        assert trusted.trust_score == 0.9
        # Trusted node ranks above the neutral one.
        assert results[0].name == "trusted"

    def test_trust_score_term_in_final_score(self):
        # Single node, isolate the trust contribution. With alpha=0,
        # beta=0, gamma=0, delta=0.1: final = 1.0 * fulltext + 0.1 * trust.
        rows = [{"type": "Concept", "name": "X", "score": 1.0, "trust_level": 0.8}]
        engine = self._engine(rows, trust_delta=0.1)
        results = engine.search("q", limit=5)
        # fulltext normalises to 1.0 (single result), trust = 0.8.
        assert results[0].final_score == pytest.approx(1.0 + 0.1 * 0.8)


# ---------------------------------------------------------------------------
# 4. Vector-path also carries trust_level when the backend exposes it
# ---------------------------------------------------------------------------


class TestTrustOnVectorPath:
    def test_vector_only_results_pick_up_trust(self):
        # Vector path emits the node with trust_level; fulltext is empty.
        v_rows = [
            {"node_id": "1", "label": "Concept", "name": "X", "score": 0.9, "trust_level": 0.9},
        ]
        engine = HybridSearchEngine(
            _MockGraph([]),
            _MockVector(v_rows),
            _MockEmbedder(),
            config=HybridConfig(
                alpha=1.0,
                graph_beta=0.0,
                temporal_gamma=0.0,
                trust_delta=0.1,
            ),
        )
        results = engine.search("q", limit=5)
        assert results[0].trust_score == 0.9
        # final = 1.0 * vector(=1.0) + 0.1 * 0.9
        assert results[0].final_score == pytest.approx(1.0 + 0.1 * 0.9)
