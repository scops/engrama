"""Tests for DDR-003 Phase E provenance fields.

Covers the Provenance dataclass, env-driven trust overrides, the engine's
default_provenance propagation, the MCP server's _with_mcp_provenance
helper, and the SDK's automatic source="sdk" tagging.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engrama.adapters.mcp.server import _with_mcp_provenance
from engrama.core.engine import EngramaEngine
from engrama.core.security import (
    DEFAULT_TRUST_LEVELS,
    Provenance,
    default_trust_for,
)

# ---------------------------------------------------------------------------
# 1. Provenance dataclass
# ---------------------------------------------------------------------------


class TestProvenanceDataclass:
    def test_known_sources_use_default_trust(self):
        for source, expected in DEFAULT_TRUST_LEVELS.items():
            prov = Provenance(source=source)
            assert prov.trust_level == expected

    def test_unknown_source_uses_neutral_half(self):
        assert default_trust_for("mystery") == 0.5
        assert Provenance(source="mystery").trust_level == 0.5

    def test_explicit_trust_level_wins(self):
        prov = Provenance(source="mcp", trust_level=0.95)
        assert prov.trust_level == 0.95

    def test_to_properties_minimum(self):
        props = Provenance(source="sdk").to_properties()
        assert props == {"source": "sdk", "trust_level": DEFAULT_TRUST_LEVELS["sdk"]}

    def test_to_properties_includes_optional_fields(self):
        props = Provenance(
            source="mcp",
            source_agent="claude-desktop",
            source_session="abc-123",
        ).to_properties()
        assert props == {
            "source": "mcp",
            "source_agent": "claude-desktop",
            "source_session": "abc-123",
            "trust_level": DEFAULT_TRUST_LEVELS["mcp"],
        }

    def test_frozen(self):
        prov = Provenance(source="sdk")
        with pytest.raises(Exception):  # FrozenInstanceError, subclass of AttributeError
            prov.source = "mcp"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. ENGRAMA_TRUST_LEVELS env override
# ---------------------------------------------------------------------------


class TestTrustEnvOverride:
    def test_env_overrides_single_source(self, monkeypatch):
        monkeypatch.setenv("ENGRAMA_TRUST_LEVELS", "mcp=0.2")
        assert default_trust_for("mcp") == 0.2
        # other sources unaffected
        assert default_trust_for("sdk") == DEFAULT_TRUST_LEVELS["sdk"]

    def test_env_overrides_multiple_sources(self, monkeypatch):
        monkeypatch.setenv("ENGRAMA_TRUST_LEVELS", "sync=1.0,cli=1.0,sdk=0.9,mcp=0.3")
        assert default_trust_for("sync") == 1.0
        assert default_trust_for("sdk") == 0.9
        assert default_trust_for("mcp") == 0.3

    def test_env_invalid_pair_is_ignored(self, monkeypatch):
        monkeypatch.setenv("ENGRAMA_TRUST_LEVELS", "mcp=not-a-float,sdk=0.7")
        assert default_trust_for("mcp") == DEFAULT_TRUST_LEVELS["mcp"]
        assert default_trust_for("sdk") == 0.7

    def test_env_blank_pair_is_ignored(self, monkeypatch):
        monkeypatch.setenv("ENGRAMA_TRUST_LEVELS", ",mcp=0.4,,")
        assert default_trust_for("mcp") == 0.4


# ---------------------------------------------------------------------------
# 3. Engine.merge_node provenance propagation
# ---------------------------------------------------------------------------


def _stub_store_returning_record():
    """Mock store whose merge_node returns a single record list."""
    store = MagicMock(spec=["merge_node"])
    store.merge_node.return_value = [{"n": {"created_at": "x", "updated_at": "x"}}]
    return store


class TestEngineMergeNodeProvenance:
    def test_no_provenance_no_fields_written(self):
        store = _stub_store_returning_record()
        engine = EngramaEngine(store)
        engine.merge_node("Concept", {"name": "Async", "notes": "n"})
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        assert "source" not in extra
        assert "trust_level" not in extra

    def test_default_provenance_is_applied(self):
        store = _stub_store_returning_record()
        engine = EngramaEngine(store, default_provenance=Provenance(source="sdk"))
        engine.merge_node("Concept", {"name": "Async"})
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        assert extra["source"] == "sdk"
        assert extra["trust_level"] == DEFAULT_TRUST_LEVELS["sdk"]

    def test_explicit_provenance_overrides_default(self):
        store = _stub_store_returning_record()
        engine = EngramaEngine(store, default_provenance=Provenance(source="sdk"))
        engine.merge_node(
            "Concept",
            {"name": "Async"},
            provenance=Provenance(source="sync"),
        )
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        assert extra["source"] == "sync"
        assert extra["trust_level"] == DEFAULT_TRUST_LEVELS["sync"]

    def test_provenance_carries_agent_and_session(self):
        store = _stub_store_returning_record()
        engine = EngramaEngine(
            store,
            default_provenance=Provenance(
                source="sdk", source_agent="agent-x", source_session="sess-1"
            ),
        )
        engine.merge_node("Concept", {"name": "Async"})
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        assert extra["source_agent"] == "agent-x"
        assert extra["source_session"] == "sess-1"


# ---------------------------------------------------------------------------
# 4. MCP _with_mcp_provenance helper
# ---------------------------------------------------------------------------


class TestMCPProvenanceHelper:
    def test_empty_extras(self):
        out = _with_mcp_provenance()
        assert out["source"] == "mcp"
        assert out["trust_level"] == DEFAULT_TRUST_LEVELS["mcp"]

    def test_merges_user_extras(self):
        out = _with_mcp_provenance({"body": "hello", "confidence": 0.7})
        assert out["body"] == "hello"
        assert out["confidence"] == 0.7
        assert out["source"] == "mcp"

    def test_none_extras(self):
        out = _with_mcp_provenance(None)
        assert out == {"source": "mcp", "trust_level": DEFAULT_TRUST_LEVELS["mcp"]}


# ---------------------------------------------------------------------------
# 5. SDK auto-tags writes as source=sdk
# ---------------------------------------------------------------------------


class TestSDKDefaultProvenance:
    def test_sdk_engine_has_sdk_provenance(self, tmp_path, monkeypatch):
        # Force SQLite to a clean tmp path so we don't touch the user's DB.
        monkeypatch.setenv("ENGRAMA_DB_PATH", str(tmp_path / "sdk-test.db"))
        # Disable embeddings to keep the test hermetic.
        monkeypatch.setenv("EMBEDDING_PROVIDER", "null")

        from engrama import Engrama

        with Engrama(backend="sqlite") as eng:
            prov = eng._engine.default_provenance
            assert prov is not None
            assert prov.source == "sdk"
            assert prov.source_agent is None
            assert prov.source_session is None

    def test_sdk_threads_agent_and_session(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENGRAMA_DB_PATH", str(tmp_path / "sdk-test.db"))
        monkeypatch.setenv("EMBEDDING_PROVIDER", "null")

        from engrama import Engrama

        with Engrama(
            backend="sqlite",
            source_agent="claude-cli",
            source_session="conv-42",
        ) as eng:
            prov = eng._engine.default_provenance
            assert prov.source == "sdk"
            assert prov.source_agent == "claude-cli"
            assert prov.source_session == "conv-42"


# ---------------------------------------------------------------------------
# 6. Obsidian sync passes source=sync
# ---------------------------------------------------------------------------


class TestObsidianSyncProvenance:
    def test_sync_module_constant_is_sync(self):
        from engrama.adapters.obsidian import sync as sync_mod

        assert sync_mod._SYNC_PROVENANCE.source == "sync"
        assert sync_mod._SYNC_PROVENANCE.trust_level == DEFAULT_TRUST_LEVELS["sync"]
