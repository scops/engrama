"""Cross-tenant isolation (Spec 001, US-2 / T-1, T-9).

Two SDK instances on one shared SQLite database, scoped to different tenants.
Neither may observe the other's nodes via search or multi-hop context — the
fail-closed scope filter is the only thing standing between them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engrama import Engrama


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in (
        "ENGRAMA_ORG_ID",
        "ENGRAMA_USER_ID",
        "ENGRAMA_AGENT_ID",
        "ENGRAMA_SESSION_ID",
        "ENGRAMA_LOCAL_SUB",
        "VAULT_PATH",
    ):
        monkeypatch.delenv(var, raising=False)


def test_search_does_not_leak_across_tenants(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    with Engrama(backend="sqlite", db_path=db, org_id="acme", user_id="alice") as eng:
        eng.remember("Concept", "demo-alice", "alice private memo about widgets")
    with Engrama(backend="sqlite", db_path=db, org_id="globex", user_id="bob") as eng:
        names = {h["name"] for h in eng.search("widgets OR memo OR demo")}
    assert "demo-alice" not in names


def test_same_user_string_isolated_by_org(tmp_path: Path) -> None:
    # Same user_id under different orgs must not collide (T-1 variant).
    db = tmp_path / "shared.db"
    with Engrama(backend="sqlite", db_path=db, org_id="acme", user_id="sam") as eng:
        eng.remember("Concept", "acme-doc", "internal acme widget notes")
    with Engrama(backend="sqlite", db_path=db, org_id="globex", user_id="sam") as eng:
        names = {h["name"] for h in eng.search("widget OR notes OR acme")}
    assert "acme-doc" not in names


def test_context_traversal_does_not_cross_tenants(tmp_path: Path) -> None:
    # A pathological cross-tenant relation must still not leak via traversal
    # (T-9). We wire the edge through the unscoped store, then read scoped.
    db = tmp_path / "shared.db"
    with Engrama(backend="sqlite", db_path=db, org_id="acme", user_id="alice") as eng:
        eng.remember("Concept", "alice-root", "alice root node")
    with Engrama(backend="sqlite", db_path=db, org_id="globex", user_id="bob") as eng:
        eng.remember("Concept", "bob-secret", "bob secret node")
        # Force a cross-tenant edge directly in the store (should never happen
        # in normal operation, but isolation must hold even if it does).
        eng._store.merge_relation(
            "Concept", "name", "bob-secret", "RELATED_TO", "Concept", "name", "alice-root"
        )

    with Engrama(backend="sqlite", db_path=db, org_id="acme", user_id="alice") as eng:
        rows = eng._store.get_neighbours(
            "Concept", "name", "alice-root", scope=eng._engine.default_scope
        )
        neighbours = {r["neighbour"].get("name") for r in rows}
    assert "bob-secret" not in neighbours
