"""Tests for DDR-003 Phase F (Roadmap P14) — multi-scope memory.

PR-F1 covers the write side only: the :class:`MemoryScope` dataclass,
the engine threading it through ``merge_node``, the sanitiser blocking
caller-supplied scope keys, and the SDK exposing scope kwargs. PR-F2
will add the matching read-side filter.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engrama.core.engine import EngramaEngine
from engrama.core.scope import MemoryScope
from engrama.core.security import RESERVED_KEYS, RESERVED_SCOPE_KEYS

# ---------------------------------------------------------------------------
# 1. MemoryScope dataclass
# ---------------------------------------------------------------------------


class TestMemoryScopeDataclass:
    def test_defaults_are_all_none(self):
        s = MemoryScope()
        assert s.org_id is None
        assert s.user_id is None
        assert s.agent_id is None
        assert s.session_id is None

    def test_to_properties_empty_when_all_none(self):
        assert MemoryScope().to_properties() == {}

    def test_to_properties_includes_only_set_dimensions(self):
        s = MemoryScope(user_id="alice", org_id="acme")
        assert s.to_properties() == {"user_id": "alice", "org_id": "acme"}

    def test_to_properties_includes_all_four(self):
        s = MemoryScope(org_id="acme", user_id="alice", agent_id="bot", session_id="conv")
        assert s.to_properties() == {
            "org_id": "acme",
            "user_id": "alice",
            "agent_id": "bot",
            "session_id": "conv",
        }

    def test_is_empty(self):
        assert MemoryScope().is_empty() is True
        assert MemoryScope(user_id="alice").is_empty() is False
        assert MemoryScope(session_id="x").is_empty() is False

    def test_frozen(self):
        s = MemoryScope(user_id="alice")
        with pytest.raises(Exception):  # FrozenInstanceError subclass of AttributeError
            s.user_id = "bob"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Sanitiser strips scope keys (same as provenance keys)
# ---------------------------------------------------------------------------


class TestSanitiserStripsScopeKeys:
    def test_scope_keys_in_reserved(self):
        assert RESERVED_SCOPE_KEYS == {"org_id", "user_id", "agent_id", "session_id"}
        # Union exposed for the sanitiser
        for key in RESERVED_SCOPE_KEYS:
            assert key in RESERVED_KEYS

    @pytest.mark.parametrize("reserved", sorted(RESERVED_SCOPE_KEYS))
    def test_caller_scope_key_is_stripped(self, reserved):
        from engrama.core.security import Sanitiser

        out = Sanitiser().sanitise_properties({"name": "X", reserved: "spoofed"})
        assert reserved not in out


# ---------------------------------------------------------------------------
# 3. Engine.merge_node persists scope as flat properties
# ---------------------------------------------------------------------------


def _stub_store():
    store = MagicMock(spec=["merge_node"])
    store.merge_node.return_value = [{"n": {"created_at": "x", "updated_at": "x"}}]
    return store


class TestEngineScopeTagging:
    def test_no_scope_no_scope_fields(self):
        store = _stub_store()
        engine = EngramaEngine(store)
        engine.merge_node("Concept", {"name": "Async"})
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        for key in RESERVED_SCOPE_KEYS:
            assert key not in extra

    def test_default_scope_is_applied(self):
        store = _stub_store()
        engine = EngramaEngine(
            store,
            default_scope=MemoryScope(user_id="alice", org_id="acme"),
        )
        engine.merge_node("Concept", {"name": "Async"})
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        assert extra["user_id"] == "alice"
        assert extra["org_id"] == "acme"
        # Unset dimensions are NOT written.
        assert "agent_id" not in extra
        assert "session_id" not in extra

    def test_explicit_scope_overrides_default(self):
        store = _stub_store()
        engine = EngramaEngine(
            store,
            default_scope=MemoryScope(user_id="alice"),
        )
        engine.merge_node(
            "Concept",
            {"name": "Async"},
            scope=MemoryScope(user_id="bob"),
        )
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        assert extra["user_id"] == "bob"

    def test_empty_explicit_scope_writes_no_scope_fields(self):
        # An explicit `MemoryScope()` is the caller saying "this write
        # is unscoped" — it shadows the default and persists nothing.
        # Callers who want the default should just not pass `scope=`.
        store = _stub_store()
        engine = EngramaEngine(
            store,
            default_scope=MemoryScope(user_id="alice"),
        )
        engine.merge_node("Concept", {"name": "Async"}, scope=MemoryScope())
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        for key in RESERVED_SCOPE_KEYS:
            assert key not in extra

    def test_caller_cannot_smuggle_scope_via_properties(self):
        store = _stub_store()
        engine = EngramaEngine(store, default_scope=MemoryScope(user_id="alice"))
        engine.merge_node(
            "Concept",
            {"name": "Async", "user_id": "bob", "org_id": "evil_corp"},
        )
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        assert extra["user_id"] == "alice"
        assert "org_id" not in extra  # default_scope didn't set org_id

    def test_empty_default_scope_writes_no_scope_fields(self):
        store = _stub_store()
        engine = EngramaEngine(store, default_scope=MemoryScope())
        engine.merge_node("Concept", {"name": "Async"})
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        for key in RESERVED_SCOPE_KEYS:
            assert key not in extra


# ---------------------------------------------------------------------------
# 4. SDK plumbs scope kwargs into the engine
# ---------------------------------------------------------------------------


class TestSDKScope:
    def test_sdk_no_scope_engine_default_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENGRAMA_DB_PATH", str(tmp_path / "scope-test.db"))
        monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
        from engrama import Engrama

        with Engrama(backend="sqlite") as eng:
            assert eng._engine.default_scope is None

    def test_sdk_threads_user_and_org(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENGRAMA_DB_PATH", str(tmp_path / "scope-test.db"))
        monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
        from engrama import Engrama

        with Engrama(backend="sqlite", user_id="alice", org_id="acme") as eng:
            scope = eng._engine.default_scope
            assert scope is not None
            assert scope.user_id == "alice"
            assert scope.org_id == "acme"
            assert scope.agent_id is None
            assert scope.session_id is None

    def test_sdk_all_none_is_treated_as_no_scope(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENGRAMA_DB_PATH", str(tmp_path / "scope-test.db"))
        monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
        from engrama import Engrama

        with Engrama(
            backend="sqlite",
            org_id=None,
            user_id=None,
            agent_id=None,
            session_id=None,
        ) as eng:
            assert eng._engine.default_scope is None
