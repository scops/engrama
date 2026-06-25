"""GDPR right-to-erasure tests (Spec 001, US-3 / T-7, T027).

``engrama_gdpr_forget(org_id, user_id, mode)`` must physically remove every
node, relation and embedding belonging to one identity, leaving every other
identity untouched, and be idempotent (a second run reports all zeros).

DESTRUCTIVE-CODE SAFETY (non-negotiable): these tests run ONLY against a
disposable per-test SQLite file under ``tmp_path``. They never touch the
shared Neo4j (which is production). The Neo4j erasure path mirrors this logic
and is exercised in CI, not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engrama import Engrama
from engrama.backends.sqlite.vector import SqliteVecStore
from engrama.migrate import gdpr_forget

# Identity under erasure vs the bystander that must survive intact.
_TARGET = {"org_id": "acme", "user_id": "alice"}
_BYSTANDER = {"org_id": "globex", "user_id": "bob"}

_EMBED_DIM = 4
_FAKE_VEC = [0.1, 0.2, 0.3, 0.4]


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


def _populate_scope(db: Path, scope: dict) -> None:
    """Write one Project, one Technology and a USES relation under ``scope``."""
    with Engrama(backend="sqlite", db_path=db, **scope) as eng:
        eng.remember("Project", f"proj-{scope['user_id']}", "a project")
        eng.remember("Technology", f"tech-{scope['user_id']}", "a technology")
        eng.associate(
            f"proj-{scope['user_id']}", "Project", "USES", f"tech-{scope['user_id']}", "Technology"
        )


def _node_ids_for_scope(conn, scope: dict) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM nodes "
        "WHERE json_extract(props, '$.org_id') = ? AND json_extract(props, '$.user_id') = ?",
        (scope["org_id"], scope["user_id"]),
    ).fetchall()
    return [r["id"] if not isinstance(r, tuple) else r[0] for r in rows]


def _counts(conn, scope: dict) -> dict:
    nodes = conn.execute(
        "SELECT COUNT(*) AS n FROM nodes "
        "WHERE json_extract(props, '$.org_id') = ? AND json_extract(props, '$.user_id') = ?",
        (scope["org_id"], scope["user_id"]),
    ).fetchone()["n"]
    edges = conn.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE org_id = ? AND user_id = ?",
        (scope["org_id"], scope["user_id"]),
    ).fetchone()["n"]
    return {"nodes": nodes, "edges": edges}


@pytest.fixture()
def populated(tmp_path: Path):
    """Two scopes' worth of data in one disposable DB, with seeded embeddings.

    Yields ``(admin_engine, vector_store)`` ready for ``gdpr_forget``. The
    embeddings are seeded directly (the env embedder is ``null``) so the
    deletion of vec0 rows can be asserted without a real model.
    """
    db = tmp_path / "gdpr.db"
    _populate_scope(db, _TARGET)
    _populate_scope(db, _BYSTANDER)

    eng = Engrama(backend="sqlite", db_path=db, **_TARGET)
    conn = eng._store._conn
    vstore = SqliteVecStore(conn, dimensions=_EMBED_DIM)
    vstore.ensure_index()
    for nid in _node_ids_for_scope(conn, _TARGET) + _node_ids_for_scope(conn, _BYSTANDER):
        vstore.store_vectors([(str(nid), _FAKE_VEC)])
    try:
        yield eng, vstore
    finally:
        eng.close()


def _embedding_count(conn, node_ids: list[int]) -> int:
    if not node_ids:
        return 0
    placeholders = ",".join("?" * len(node_ids))
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM node_embeddings WHERE node_id IN ({placeholders})",
        node_ids,
    ).fetchone()["n"]


def _fts_count(conn, node_ids: list[int]) -> int:
    if not node_ids:
        return 0
    placeholders = ",".join("?" * len(node_ids))
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM nodes_fts WHERE rowid IN ({placeholders})",
        node_ids,
    ).fetchone()["n"]


def test_requires_exactly_one_mode(populated) -> None:
    eng, vstore = populated
    with pytest.raises(ValueError):
        gdpr_forget(eng._store, vstore, **_TARGET)  # neither dry_run nor apply
    with pytest.raises(ValueError):
        gdpr_forget(eng._store, vstore, **_TARGET, dry_run=True, apply=True)


def test_dry_run_reports_without_deleting(populated) -> None:
    eng, vstore = populated
    conn = eng._store._conn

    report = gdpr_forget(eng._store, vstore, **_TARGET, dry_run=True)

    assert report["org_id"] == _TARGET["org_id"]
    assert report["user_id"] == _TARGET["user_id"]
    assert report["deleted_nodes_by_label"] == {"Project": 1, "Technology": 1}
    assert report["deleted_relations"] == 1
    assert report["deleted_embeddings"] == 2
    # Nothing was actually removed.
    assert _counts(conn, _TARGET) == {"nodes": 2, "edges": 1}


def test_apply_erases_target_scope_completely(populated) -> None:
    eng, vstore = populated
    conn = eng._store._conn
    target_ids = _node_ids_for_scope(conn, _TARGET)

    report = gdpr_forget(eng._store, vstore, **_TARGET, apply=True)

    assert report["deleted_nodes_by_label"] == {"Project": 1, "Technology": 1}
    assert report["deleted_relations"] == 1
    assert report["deleted_embeddings"] == 2
    # Graph + vec0 residue for the target identity is zero.
    assert _counts(conn, _TARGET) == {"nodes": 0, "edges": 0}
    assert _embedding_count(conn, target_ids) == 0


def test_apply_leaves_other_scope_intact(populated) -> None:
    eng, vstore = populated
    conn = eng._store._conn
    bystander_ids = _node_ids_for_scope(conn, _BYSTANDER)

    gdpr_forget(eng._store, vstore, **_TARGET, apply=True)

    assert _counts(conn, _BYSTANDER) == {"nodes": 2, "edges": 1}
    assert _embedding_count(conn, bystander_ids) == 2


def test_apply_is_idempotent(populated) -> None:
    eng, vstore = populated

    gdpr_forget(eng._store, vstore, **_TARGET, apply=True)
    second = gdpr_forget(eng._store, vstore, **_TARGET, apply=True)

    assert second["deleted_nodes_by_label"] == {}
    assert second["deleted_relations"] == 0
    assert second["deleted_embeddings"] == 0


def test_apply_purges_fulltext_index_rows(populated) -> None:
    """Erasure must also remove the subject's rows from the content-storing FTS5
    table — otherwise the indexed PII (name/title/description/…) survives on disk
    after a 'permanent' delete, violating Art. 17. The bystander stays indexed.
    """
    eng, vstore = populated
    conn = eng._store._conn
    target_ids = _node_ids_for_scope(conn, _TARGET)
    bystander_ids = _node_ids_for_scope(conn, _BYSTANDER)

    # Precondition: the target's text is indexed before erasure.
    assert _fts_count(conn, target_ids) == 2

    gdpr_forget(eng._store, vstore, **_TARGET, apply=True)

    # The erased subject leaves no searchable text behind; bystander intact.
    assert _fts_count(conn, target_ids) == 0
    assert _fts_count(conn, bystander_ids) == 2


# --------------------------------------------------------------------------
# T030 — internal-vault note deletion by identity
# --------------------------------------------------------------------------


def _seed_note(vault: Path, name: str) -> str:
    """Write a vault note and return its vault-relative path."""
    (vault / name).write_text(f"---\nengrama_id: {name}\n---\n# {name}\n", encoding="utf-8")
    return name


def _stamp_obsidian_path(db: Path, scope: dict, rel_path: str) -> None:
    """Attach ``obsidian_path`` to every node of ``scope`` in ``db``."""
    with Engrama(backend="sqlite", db_path=db, **scope) as eng:
        eng._store._conn.execute(
            "UPDATE nodes SET props = json_set(props, '$.obsidian_path', ?) "
            "WHERE json_extract(props, '$.org_id') = ? "
            "AND json_extract(props, '$.user_id') = ?",
            (rel_path, scope["org_id"], scope["user_id"]),
        )
        eng._store._conn.commit()


@pytest.fixture()
def vaulted(tmp_path: Path):
    """A disposable vault + DB with one note per identity, linked via
    ``obsidian_path`` on the scope's nodes. Yields ``(sync, vault)``."""
    from engrama.adapters.obsidian.adapter import ObsidianAdapter
    from engrama.adapters.obsidian.sync import ObsidianSync

    vault = tmp_path / "vault"
    vault.mkdir()
    target_note = _seed_note(vault, "alice-note.md")
    bystander_note = _seed_note(vault, "bob-note.md")

    db = tmp_path / "vault-gdpr.db"
    _populate_scope(db, _TARGET)
    _populate_scope(db, _BYSTANDER)
    _stamp_obsidian_path(db, _TARGET, target_note)
    _stamp_obsidian_path(db, _BYSTANDER, bystander_note)

    eng = Engrama(backend="sqlite", db_path=db, **_TARGET)
    sync = ObsidianSync(eng, ObsidianAdapter(vault_path=vault))
    try:
        yield sync, vault
    finally:
        eng.close()


def test_dry_run_counts_notes_without_deleting(vaulted) -> None:
    sync, vault = vaulted
    assert sync.delete_notes_for_scope(**_TARGET, apply=False) == 1
    assert (vault / "alice-note.md").exists()
    assert (vault / "bob-note.md").exists()


def test_apply_deletes_only_target_notes(vaulted) -> None:
    sync, vault = vaulted
    assert sync.delete_notes_for_scope(**_TARGET, apply=True) == 1
    assert not (vault / "alice-note.md").exists()
    assert (vault / "bob-note.md").exists()


def test_note_deletion_is_idempotent(vaulted) -> None:
    sync, vault = vaulted
    sync.delete_notes_for_scope(**_TARGET, apply=True)
    assert sync.delete_notes_for_scope(**_TARGET, apply=True) == 0
