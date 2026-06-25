"""Cross-tenant isolation for the context (root-node) read path.

Regression for a cross-tenant disclosure: ``get_node_with_neighbours`` (backing
the ``engrama_context`` MCP tool) scope-filtered only the *neighbours*, leaving
the **root node** unscoped — so a resolved tenant could read another tenant's
node, with all its enrichment fields, by guessing its (label, key).

The fix: when a scope is supplied, the root node must be visible at that scope;
a ``None`` scope keeps the admin/debug fetch-by-key. SQLite backend (no Neo4j
required); the Neo4j path mirrors this and is covered in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engrama import Engrama
from engrama.core.scope import ENTITY_SENTINEL, MemoryScope, node_visible

_OWNER = {"org_id": "globex", "user_id": "bob"}
_OTHER = MemoryScope(org_id="acme", user_id="alice")
_OWNER_SCOPE = MemoryScope(org_id="globex", user_id="bob")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in ("ENGRAMA_ORG_ID", "ENGRAMA_USER_ID", "ENGRAMA_LOCAL_SUB", "VAULT_PATH"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def store_with_owned_node(tmp_path: Path):
    """A disposable SQLite store holding one Project owned by ``_OWNER``."""
    db = tmp_path / "ctx.db"
    with Engrama(backend="sqlite", db_path=db, **_OWNER) as eng_owner:
        eng_owner.remember("Project", "secret-proj", "confidential roadmap")
    eng = Engrama(backend="sqlite", db_path=db, **_OWNER)
    try:
        yield eng._store
    finally:
        eng.close()


def test_other_tenant_cannot_read_root_node(store_with_owned_node) -> None:
    store = store_with_owned_node
    out = store.get_node_with_neighbours("Project", "name", "secret-proj", scope=_OTHER)
    assert out is None


def test_owner_can_read_root_node(store_with_owned_node) -> None:
    store = store_with_owned_node
    out = store.get_node_with_neighbours("Project", "name", "secret-proj", scope=_OWNER_SCOPE)
    assert out is not None
    assert out["node"]["name"] == "secret-proj"


def test_none_scope_keeps_admin_fetch_by_key(store_with_owned_node) -> None:
    store = store_with_owned_node
    out = store.get_node_with_neighbours("Project", "name", "secret-proj", scope=None)
    assert out is not None
    assert out["node"]["name"] == "secret-proj"


def test_incomplete_scope_fails_closed(store_with_owned_node) -> None:
    store = store_with_owned_node
    partial = MemoryScope(org_id="globex", user_id="")
    out = store.get_node_with_neighbours("Project", "name", "secret-proj", scope=partial)
    assert out is None


# --- node_visible predicate (pure) ---------------------------------------


def test_node_visible_same_identity() -> None:
    assert node_visible(_OWNER_SCOPE, "globex", "bob") is True


def test_node_visible_org_shared_sentinel() -> None:
    assert node_visible(_OWNER_SCOPE, "globex", ENTITY_SENTINEL) is True


def test_node_visible_other_tenant() -> None:
    assert node_visible(_OWNER_SCOPE, "acme", "alice") is False
    assert node_visible(_OWNER_SCOPE, "globex", "carol") is False


def test_node_visible_fails_closed_on_none_or_incomplete() -> None:
    assert node_visible(None, "globex", "bob") is False
    assert node_visible(MemoryScope(org_id="globex", user_id=""), "globex", "bob") is False
