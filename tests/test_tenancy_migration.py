"""Spec 001 T039 / T-10 — tenancy backfill migration.

A pre-Spec-001 SQLite database has nodes and edges with no ``org_id`` /
``user_id`` properties. Under fail-closed reads, those rows are
invisible to every scope. ``migrate_tenancy`` backfills the chosen
owner identity onto every node/relation that lacks it, and purges true
orphans (nodes that even after the backfill have no merge key — a
corruption signal, not real data).

Contract:

* **dry-run** counts and samples what would change, never writes.
* **apply** stamps ``(owner_sub, owner_sub)`` onto every identity-less
  node and relation. Idempotent: a second apply is a no-op.
* After **apply**, every prior node is reachable under
  ``MemoryScope(org_id=owner_sub, user_id=owner_sub)``.

Local-safe: every test uses a tmp SQLite DB. The Neo4j side mirrors
this shape and runs in CI.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from engrama.backends.sqlite import SqliteGraphStore
from engrama.core.scope import MemoryScope
from engrama.migrate import migrate_tenancy


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


_OWNER = "migration-test-sub"


def _seed_pre_spec_nodes(store: SqliteGraphStore) -> None:
    """Insert nodes the way a pre-Spec-001 build did: no scope props."""
    now = "2026-04-01T00:00:00+00:00"
    # Use the raw connection to bypass the engine fail-closed guard so we
    # can simulate the legacy state on disk.
    conn = store._conn
    conn.execute(
        "INSERT INTO nodes(label, key_field, key_value, props, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Project", "name", "legacy-alpha", json.dumps({"status": "active"}), now, now),
    )
    conn.execute(
        "INSERT INTO nodes(label, key_field, key_value, props, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Technology", "name", "legacy-tech", json.dumps({}), now, now),
    )
    # A true orphan: identity-less AND missing the merge-key value.
    conn.execute(
        "INSERT INTO nodes(label, key_field, key_value, props, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Concept", "name", "", json.dumps({}), now, now),
    )
    # A relation between the two legitimate nodes — also unscoped.
    f_id = conn.execute(
        "SELECT id FROM nodes WHERE label = 'Project' AND key_value = 'legacy-alpha'"
    ).fetchone()["id"]
    t_id = conn.execute(
        "SELECT id FROM nodes WHERE label = 'Technology' AND key_value = 'legacy-tech'"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO edges(from_id, rel_type, to_id, created_at) VALUES (?, ?, ?, ?)",
        (f_id, "USES", t_id, now),
    )
    conn.commit()


@pytest.fixture()
def legacy_store(tmp_path: Path) -> SqliteGraphStore:
    s = SqliteGraphStore(tmp_path / "legacy.db")
    _seed_pre_spec_nodes(s)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_reports_counts_and_does_not_write(legacy_store: SqliteGraphStore) -> None:
    report = migrate_tenancy(legacy_store, owner_sub=_OWNER, dry_run=True)
    assert report["dry_run"] is True
    # ``nodes_to_stamp`` is the action count: real rows that will be
    # backfilled. The empty-key Concept is reported separately under
    # ``orphans_to_purge`` because it's deleted, not stamped.
    assert report["nodes_to_stamp"] == 2  # alpha + tech
    assert report["relations_to_stamp"] == 1  # USES edge
    assert report["orphans_to_purge"] == 1  # the empty-key Concept
    sample = report.get("sample") or {}
    # The legitimate nodes are surfaced in the stamp sample (any order).
    stamp_names = {n["key_value"] for n in sample.get("nodes_to_stamp", [])}
    assert {"legacy-alpha", "legacy-tech"} <= stamp_names
    # Nothing changed on disk: a read under the would-be scope still sees 0.
    scope = MemoryScope(org_id=_OWNER, user_id=_OWNER)
    assert legacy_store.list_existing_nodes(scope=scope) == []


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_stamps_identity_and_purges_orphans(legacy_store: SqliteGraphStore) -> None:
    report = migrate_tenancy(legacy_store, owner_sub=_OWNER, apply=True)
    assert report["dry_run"] is False
    assert report["nodes_stamped"] == 2  # the two real nodes
    assert report["relations_stamped"] == 1
    assert report["orphans_purged"] == 1

    scope = MemoryScope(org_id=_OWNER, user_id=_OWNER)
    visible = {n["name"] for n in legacy_store.list_existing_nodes(scope=scope)}
    assert {"legacy-alpha", "legacy-tech"} <= visible

    # The orphan must be gone.
    row = legacy_store._conn.execute(
        "SELECT COUNT(*) AS n FROM nodes WHERE label = 'Concept' AND key_value = ''"
    ).fetchone()
    assert row["n"] == 0

    # The relation now carries identity, so a relation-scoped read filter
    # (FR-1) would let it through.
    row = legacy_store._conn.execute("SELECT org_id, user_id FROM edges LIMIT 1").fetchone()
    assert row["org_id"] == _OWNER
    assert row["user_id"] == _OWNER


def test_apply_is_idempotent(legacy_store: SqliteGraphStore) -> None:
    """A second ``apply`` after a successful first one is a no-op — no
    new rows stamped, no fresh orphans purged.
    """
    first = migrate_tenancy(legacy_store, owner_sub=_OWNER, apply=True)
    assert first["nodes_stamped"] >= 1
    second = migrate_tenancy(legacy_store, owner_sub=_OWNER, apply=True)
    assert second["nodes_stamped"] == 0
    assert second["relations_stamped"] == 0
    assert second["orphans_purged"] == 0


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_requires_exactly_one_mode(legacy_store: SqliteGraphStore) -> None:
    """Passing neither ``dry_run`` nor ``apply`` (or both) is a usage
    error — the migration is destructive enough that the caller must say
    which mode they want.
    """
    with pytest.raises(ValueError, match="dry_run|apply"):
        migrate_tenancy(legacy_store, owner_sub=_OWNER)
    with pytest.raises(ValueError, match="dry_run|apply"):
        migrate_tenancy(legacy_store, owner_sub=_OWNER, dry_run=True, apply=True)


def test_rejects_empty_owner_sub(legacy_store: SqliteGraphStore) -> None:
    with pytest.raises(ValueError, match="owner_sub"):
        migrate_tenancy(legacy_store, owner_sub="", dry_run=True)
    with pytest.raises(ValueError, match="owner_sub"):
        migrate_tenancy(legacy_store, owner_sub="   ", apply=True)


def test_preserves_already_stamped_rows(tmp_path: Path) -> None:
    """Rows that ALREADY carry a different identity must not be
    re-stamped — those are real data belonging to another tenant and a
    backfill that overwrote them would be a leak.
    """
    s = SqliteGraphStore(tmp_path / "mixed.db")
    try:
        # Insert one already-scoped node and one identity-less node.
        now = "2026-04-01T00:00:00+00:00"
        s._conn.execute(
            "INSERT INTO nodes(label, key_field, key_value, props, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "Project",
                "name",
                "owned-by-bob",
                json.dumps({"org_id": "bob", "user_id": "bob"}),
                now,
                now,
            ),
        )
        s._conn.execute(
            "INSERT INTO nodes(label, key_field, key_value, props, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Project", "name", "legacy-unowned", json.dumps({}), now, now),
        )
        s._conn.commit()

        migrate_tenancy(s, owner_sub=_OWNER, apply=True)

        bob_props = json.loads(
            s._conn.execute("SELECT props FROM nodes WHERE key_value = 'owned-by-bob'").fetchone()[
                "props"
            ]
        )
        new_props = json.loads(
            s._conn.execute(
                "SELECT props FROM nodes WHERE key_value = 'legacy-unowned'"
            ).fetchone()["props"]
        )
        assert bob_props["org_id"] == "bob" and bob_props["user_id"] == "bob"
        assert new_props["org_id"] == _OWNER and new_props["user_id"] == _OWNER
    finally:
        s.close()


def test_apply_against_already_clean_store_is_zero_work(tmp_path: Path) -> None:
    """Fresh DB (no pre-Spec-001 rows) is already clean — applying the
    migration writes nothing.
    """
    s = SqliteGraphStore(tmp_path / "clean.db")
    try:
        report = migrate_tenancy(s, owner_sub=_OWNER, apply=True)
        assert report["nodes_stamped"] == 0
        assert report["relations_stamped"] == 0
        assert report["orphans_purged"] == 0
    finally:
        s.close()


def test_dry_run_handles_missing_vec_table(tmp_path: Path) -> None:
    """Some legacy DBs never had ``node_embeddings``. The dry-run must
    not assume the table exists.
    """
    s = SqliteGraphStore(tmp_path / "novec.db")
    try:
        # Drop the vec table if it was created by schema init — simulates
        # an older install that ran without sqlite-vec.
        try:
            s._conn.execute("DROP TABLE node_embeddings")
            s._conn.commit()
        except sqlite3.OperationalError:
            pass
        _seed_pre_spec_nodes(s)
        report = migrate_tenancy(s, owner_sub=_OWNER, dry_run=True)
        assert report["nodes_to_stamp"] >= 2
    finally:
        s.close()
