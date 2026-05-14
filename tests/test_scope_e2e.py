"""End-to-end multi-scope memory tests (DDR-003 Phase F / Roadmap P14).

Exercises the full SDK pipeline: two Engrama instances on the same
SQLite database, scoped to different users. Alice's writes go through
``engine.merge_node`` with her scope properties; Bob's instance queries
through ``hybrid_search`` / ``recall`` and never sees Alice's nodes —
unless they were written at a broader scope (org-level, global).
"""

from __future__ import annotations

import pytest

from engrama.core.scope import MemoryScope

# ---------------------------------------------------------------------------
# 1. MemoryScope.from_env
# ---------------------------------------------------------------------------


class TestMemoryScopeFromEnv:
    def test_no_env_returns_empty_scope(self, monkeypatch):
        for var in (
            "ENGRAMA_ORG_ID",
            "ENGRAMA_USER_ID",
            "ENGRAMA_AGENT_ID",
            "ENGRAMA_SESSION_ID",
        ):
            monkeypatch.delenv(var, raising=False)
        scope = MemoryScope.from_env()
        assert scope.is_empty()

    def test_env_populates_dimensions(self, monkeypatch):
        monkeypatch.setenv("ENGRAMA_ORG_ID", "acme")
        monkeypatch.setenv("ENGRAMA_USER_ID", "alice")
        monkeypatch.delenv("ENGRAMA_AGENT_ID", raising=False)
        monkeypatch.delenv("ENGRAMA_SESSION_ID", raising=False)
        scope = MemoryScope.from_env()
        assert scope.org_id == "acme"
        assert scope.user_id == "alice"
        assert scope.agent_id is None
        assert scope.session_id is None

    def test_explicit_environ_arg_overrides_os_environ(self, monkeypatch):
        monkeypatch.setenv("ENGRAMA_USER_ID", "from-os")
        scope = MemoryScope.from_env({"ENGRAMA_USER_ID": "from-arg"})
        assert scope.user_id == "from-arg"

    def test_empty_string_treated_as_unset(self, monkeypatch):
        # Empty env vars should not be turned into "" dimensions —
        # otherwise an `export ENGRAMA_USER_ID=` would silently scope
        # the deployment to a user named "" and isolate it from itself.
        monkeypatch.setenv("ENGRAMA_USER_ID", "")
        scope = MemoryScope.from_env()
        assert scope.user_id is None


# ---------------------------------------------------------------------------
# 2. SDK auto-env: Engrama() picks scope from env when no kwargs given
# ---------------------------------------------------------------------------


@pytest.fixture()
def _hermetic_env(monkeypatch, tmp_path):
    """Shared fixture: clean scope env + tmp SQLite path + no embedder."""
    for var in (
        "ENGRAMA_ORG_ID",
        "ENGRAMA_USER_ID",
        "ENGRAMA_AGENT_ID",
        "ENGRAMA_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ENGRAMA_DB_PATH", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    return monkeypatch


class TestSDKAutoEnv:
    def test_no_env_no_kwargs_means_unscoped(self, _hermetic_env):
        from engrama import Engrama

        with Engrama(backend="sqlite") as eng:
            assert eng._engine.default_scope is None

    def test_env_only_populates_default_scope(self, _hermetic_env):
        _hermetic_env.setenv("ENGRAMA_USER_ID", "alice")
        _hermetic_env.setenv("ENGRAMA_ORG_ID", "acme")
        from engrama import Engrama

        with Engrama(backend="sqlite") as eng:
            scope = eng._engine.default_scope
            assert scope is not None
            assert scope.user_id == "alice"
            assert scope.org_id == "acme"

    def test_explicit_kwargs_bypass_env(self, _hermetic_env):
        # Operator's env says alice; the caller explicitly passes bob.
        # The explicit kwarg wins — tests + scripts can override
        # process env per Engrama instance.
        _hermetic_env.setenv("ENGRAMA_USER_ID", "alice")
        from engrama import Engrama

        with Engrama(backend="sqlite", user_id="bob") as eng:
            assert eng._engine.default_scope is not None
            assert eng._engine.default_scope.user_id == "bob"

    def test_any_explicit_kwarg_disables_env_fallback(self, _hermetic_env):
        # If the caller passes ANY scope kwarg, env is ignored entirely
        # — explicit takes over. (Mixing the two would be confusing.)
        _hermetic_env.setenv("ENGRAMA_USER_ID", "alice")
        _hermetic_env.setenv("ENGRAMA_ORG_ID", "acme")
        from engrama import Engrama

        with Engrama(backend="sqlite", session_id="conv-1") as eng:
            scope = eng._engine.default_scope
            assert scope is not None
            assert scope.session_id == "conv-1"
            assert scope.user_id is None
            assert scope.org_id is None


# ---------------------------------------------------------------------------
# 3. End-to-end isolation: two SDK instances on the same DB
# ---------------------------------------------------------------------------


@pytest.fixture()
def shared_db(monkeypatch, tmp_path):
    """Shared SQLite path so alice and bob hit the same file."""
    db = tmp_path / "shared.db"
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in (
        "ENGRAMA_ORG_ID",
        "ENGRAMA_USER_ID",
        "ENGRAMA_AGENT_ID",
        "ENGRAMA_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    return db


class TestSDKMultiUserIsolation:
    def test_alice_does_not_see_bobs_writes(self, shared_db):
        from engrama import Engrama

        with Engrama(backend="sqlite", db_path=shared_db, user_id="alice") as eng:
            eng.remember("Concept", "alice_thing", "alice's private memo")

        with Engrama(backend="sqlite", db_path=shared_db, user_id="bob") as eng:
            eng.remember("Concept", "bob_thing", "bob's private memo")
            hits = eng.search("memo")

        bob_names = {h["name"] for h in hits}
        assert "bob_thing" in bob_names
        assert "alice_thing" not in bob_names

    def test_alice_sees_org_shared_and_global(self, shared_db):
        from engrama import Engrama

        # Org-level write — no user, only org.
        with Engrama(backend="sqlite", db_path=shared_db, org_id="acme") as eng:
            eng.remember("Concept", "org_handbook", "the acme handbook")

        # Global write — no scope at all.
        with Engrama(backend="sqlite", db_path=shared_db) as eng:
            eng.remember("Concept", "global_doc", "the public docs")

        # Alice writes her own + sees everything from her org and global.
        with Engrama(backend="sqlite", db_path=shared_db, user_id="alice", org_id="acme") as eng:
            eng.remember("Concept", "alice_note", "alice's private")
            names = {h["name"] for h in eng.search("handbook OR docs OR private")}

        assert "alice_note" in names
        assert "org_handbook" in names
        assert "global_doc" in names

    def test_other_org_isolated(self, shared_db):
        from engrama import Engrama

        with Engrama(backend="sqlite", db_path=shared_db, org_id="acme") as eng:
            eng.remember("Concept", "acme_secret", "internal acme stuff")

        with Engrama(backend="sqlite", db_path=shared_db, org_id="other") as eng:
            names = {h["name"] for h in eng.search("acme stuff internal")}

        assert "acme_secret" not in names

    def test_unscoped_caller_sees_everything(self, shared_db):
        from engrama import Engrama

        with Engrama(backend="sqlite", db_path=shared_db, user_id="alice") as eng:
            eng.remember("Concept", "alice_only", "alice private")
        with Engrama(backend="sqlite", db_path=shared_db, user_id="bob") as eng:
            eng.remember("Concept", "bob_only", "bob private")

        # Admin / unscoped Engrama sees both — useful for export, audit
        # and reindex paths that need to see the full graph.
        with Engrama(backend="sqlite", db_path=shared_db) as eng:
            names = {h["name"] for h in eng.search("private")}
        assert {"alice_only", "bob_only"}.issubset(names)
