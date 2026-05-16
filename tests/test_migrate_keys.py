"""Tests for ``engrama migrate keys`` (#54).

The migration heals rows that pre-#53 wrote under the non-canonical
merge key. It's driven via :func:`engrama.migrate.migrate_keys` so the
backend-specific apply logic is exercised end-to-end. These tests run
against SQLite only — the Neo4j path lives behind the
``NEO4J_PASSWORD`` gate the rest of the suite uses and is covered by
the contract layer when the env is set.

Coverage:
* dry-run reports a plan without writing.
* apply rewrites the row in place and is idempotent.
* the canonical key shows up in the stored props.
* the alias key is dropped when it pointed at the same value, but
  preserved when it points at a different value (manual cleanup).
* the migration also covers the symmetric direction (name-keyed
  label with a stray ``title``).
* ``--labels`` scopes the run.
"""

from __future__ import annotations

import json

import pytest

from engrama.backends.sqlite import SqliteGraphStore
from engrama.migrate import detect_misnamed_keys, migrate_keys


@pytest.fixture()
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "migrate-keys.db")
    yield s
    s.close()


def _seed_misnamed(store: SqliteGraphStore, label: str, alias_key: str, key_value: str) -> int:
    """Insert a row that pretends an old write picked the wrong merge key.

    Mirrors what ``SqliteGraphStore.merge_node`` would have done pre-#53
    when the caller put ``name`` in the bag for a title-keyed label
    (or ``title`` for a name-keyed label).
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    props = {alias_key: key_value, "notes": f"seeded {label} via {alias_key}"}
    cur = store._conn.execute(
        "INSERT INTO nodes(label, key_field, key_value, props, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (label, alias_key, key_value, json.dumps(props), now, now),
    )
    store._conn.commit()
    return cur.lastrowid


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------


def test_detect_finds_misnamed_title_keyed_row(store: SqliteGraphStore) -> None:
    _seed_misnamed(store, "Experiment", "name", "smoke-2026-05-15")
    plan = detect_misnamed_keys(store, labels=["Experiment"])

    assert len(plan) == 1
    entry = plan[0]
    assert entry["label"] == "Experiment"
    assert entry["current_key_field"] == "name"
    assert entry["canonical_key_field"] == "title"
    assert entry["key_value"] == "smoke-2026-05-15"
    assert entry["conflict"] is False


def test_detect_finds_stray_title_on_name_keyed_label(store: SqliteGraphStore) -> None:
    """Symmetric direction: Concept is name-keyed; a stray ``title``
    is also misnamed."""
    _seed_misnamed(store, "Concept", "title", "wrong-key")
    plan = detect_misnamed_keys(store, labels=["Concept"])

    assert len(plan) == 1
    assert plan[0]["current_key_field"] == "title"
    assert plan[0]["canonical_key_field"] == "name"


def test_detect_skips_canonical_rows(store: SqliteGraphStore) -> None:
    """A row already written under the canonical key shouldn't appear
    in the plan."""
    store.merge_node("Experiment", "title", "already-canonical", {})
    plan = detect_misnamed_keys(store, labels=["Experiment"])
    assert plan == []


# ----------------------------------------------------------------------
# Dry-run
# ----------------------------------------------------------------------


def test_dry_run_writes_nothing(store: SqliteGraphStore) -> None:
    _seed_misnamed(store, "Experiment", "name", "smoke-dry")
    summary = migrate_keys(store, labels=["Experiment"], apply=False)

    assert summary["dry_run"] is True
    assert summary["renamed"] == 1
    assert summary["skipped_conflict"] == 0
    assert len(summary["plan"]) == 1

    # Row still misnamed because we didn't apply.
    cur = store._conn.execute(
        "SELECT key_field FROM nodes WHERE label = ? AND key_value = ?",
        ("Experiment", "smoke-dry"),
    )
    assert cur.fetchone()["key_field"] == "name"


# ----------------------------------------------------------------------
# Apply
# ----------------------------------------------------------------------


def test_apply_rewrites_key_field_and_props(store: SqliteGraphStore) -> None:
    _seed_misnamed(store, "Experiment", "name", "smoke-apply")
    summary = migrate_keys(store, labels=["Experiment"], apply=True)

    assert summary["dry_run"] is False
    assert summary["renamed"] == 1

    cur = store._conn.execute(
        "SELECT key_field, props FROM nodes WHERE label = ? AND key_value = ?",
        ("Experiment", "smoke-apply"),
    )
    row = cur.fetchone()
    assert row["key_field"] == "title"
    props = json.loads(row["props"])
    assert props["title"] == "smoke-apply"
    # The alias was pointing at the same value, so it gets dropped.
    assert "name" not in props
    # Non-key properties are preserved.
    assert props["notes"] == "seeded Experiment via name"


def test_apply_idempotent(store: SqliteGraphStore) -> None:
    """A second run should be a no-op."""
    _seed_misnamed(store, "Experiment", "name", "smoke-idempotent")
    migrate_keys(store, labels=["Experiment"], apply=True)
    second = migrate_keys(store, labels=["Experiment"], apply=True)
    assert second["renamed"] == 0
    assert second["plan"] == []


def test_apply_preserves_alias_pointing_elsewhere(store: SqliteGraphStore) -> None:
    """If a misnamed row also has the *other* key set to a different
    value, the alias is preserved verbatim — that's a manual cleanup
    case the migration shouldn't second-guess."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    props = {"name": "different-name", "notes": "weird seeded row"}
    store._conn.execute(
        "INSERT INTO nodes(label, key_field, key_value, props, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Experiment", "name", "key-value", json.dumps(props), now, now),
    )
    store._conn.commit()

    migrate_keys(store, labels=["Experiment"], apply=True)

    cur = store._conn.execute(
        "SELECT key_field, props FROM nodes WHERE label = ? AND key_value = ?",
        ("Experiment", "key-value"),
    )
    row = cur.fetchone()
    assert row["key_field"] == "title"
    props_after = json.loads(row["props"])
    assert props_after["title"] == "key-value"
    # The alias pointed elsewhere — preserved for manual review.
    assert props_after.get("name") == "different-name"


# ----------------------------------------------------------------------
# Labels scoping
# ----------------------------------------------------------------------


def test_labels_filter_limits_scope(store: SqliteGraphStore) -> None:
    _seed_misnamed(store, "Experiment", "name", "in-scope")
    _seed_misnamed(store, "Decision", "name", "out-of-scope")

    summary = migrate_keys(store, labels=["Experiment"], apply=True)
    assert summary["renamed"] == 1

    # Decision row remained untouched.
    cur = store._conn.execute(
        "SELECT key_field FROM nodes WHERE label = ? AND key_value = ?",
        ("Decision", "out-of-scope"),
    )
    assert cur.fetchone()["key_field"] == "name"

    # Now sweep everything.
    sweep = migrate_keys(store, apply=True)
    assert sweep["renamed"] == 1


# ----------------------------------------------------------------------
# Healthy graph
# ----------------------------------------------------------------------


def test_no_misnamed_rows_returns_empty_plan(store: SqliteGraphStore) -> None:
    """On a graph written entirely with the canonical key, the
    migration should find nothing and report no errors."""
    store.merge_node("Experiment", "title", "ok-1", {})
    store.merge_node("Concept", "name", "ok-2", {})

    summary = migrate_keys(store, apply=True)
    assert summary["renamed"] == 0
    assert summary["plan"] == []
    assert summary["errors"] == []


# ----------------------------------------------------------------------
# CLI plumbing
# ----------------------------------------------------------------------


def test_cli_dry_run_prints_plan(tmp_path, monkeypatch, capsys) -> None:
    """The CLI subcommand wires through to ``migrate_keys`` with the
    expected flag defaults and prints a human-readable summary."""
    db = tmp_path / "cli-engrama.db"
    monkeypatch.setenv("ENGRAMA_DB_PATH", str(db))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")

    seed = SqliteGraphStore(db)
    _seed_misnamed(seed, "Experiment", "name", "cli-dry")
    seed.close()

    from engrama.cli import cmd_migrate_keys

    args = type("Args", (), {"apply": False, "labels": None, "report": None})()
    rc = cmd_migrate_keys(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "Would rename 1 row" in out
    assert "Experiment" in out


def test_cli_apply_rewrites_and_report(tmp_path, monkeypatch, capsys) -> None:
    db = tmp_path / "cli-engrama.db"
    report_path = tmp_path / "report.json"
    monkeypatch.setenv("ENGRAMA_DB_PATH", str(db))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")

    seed = SqliteGraphStore(db)
    _seed_misnamed(seed, "Decision", "name", "cli-apply")
    seed.close()

    from engrama.cli import cmd_migrate_keys

    args = type(
        "Args",
        (),
        {"apply": True, "labels": "Decision", "report": str(report_path)},
    )()
    rc = cmd_migrate_keys(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "Renamed 1 row" in out
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["dry_run"] is False
    assert report["renamed"] == 1

    # Verify the actual row was rewritten.
    check = SqliteGraphStore(db)
    try:
        cur = check._conn.execute(
            "SELECT key_field FROM nodes WHERE label = ? AND key_value = ?",
            ("Decision", "cli-apply"),
        )
        assert cur.fetchone()["key_field"] == "title"
    finally:
        check.close()
