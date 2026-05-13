"""
Tests for ``engrama export`` / ``engrama import`` and the underlying
:mod:`engrama.migrate` module.

Round-trips run against the SQLite backend (zero-dep, runs in the
``test-sqlite`` CI job). Cross-backend SQLite ↔ Neo4j is exercised in
:mod:`tests.test_export_import_cross_backend` which requires a live
Neo4j and is part of the ``test-neo4j`` CI job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engrama.backends.sqlite import SqliteGraphStore
from engrama.backends.sqlite.vector import SqliteVecStore
from engrama.migrate import (
    EXPORT_FORMAT_VERSION,
    export_graph,
    import_graph,
)


@pytest.fixture()
def source_store(tmp_path: Path):
    s = SqliteGraphStore(tmp_path / "source.db")
    yield s
    s.close()


@pytest.fixture()
def target_store(tmp_path: Path):
    s = SqliteGraphStore(tmp_path / "target.db")
    yield s
    s.close()


def _seed(store: SqliteGraphStore) -> None:
    """Populate three nodes and two edges."""
    store.merge_node("Concept", "name", "Alpha", {"description": "first"})
    store.merge_node("Concept", "name", "Beta", {"description": "second"})
    store.merge_node("Project", "name", "P1", {"status": "active"})
    store.merge_relation("Concept", "name", "Alpha", "USES", "Project", "name", "P1")
    store.merge_relation("Concept", "name", "Beta", "USES", "Project", "name", "P1")


# ----------------------------------------------------------------------
# Envelope
# ----------------------------------------------------------------------


def test_envelope_carries_format_version_and_metadata(
    source_store: SqliteGraphStore, tmp_path: Path
):
    _seed(source_store)
    counts = export_graph(
        source_store,
        SqliteVecStore(source_store._conn, dimensions=0),  # type: ignore[attr-defined]
        tmp_path / "dump.ndjson",
    )
    assert counts == {"nodes": 3, "relations": 2, "vectors": 0}

    envelope = json.loads((tmp_path / "dump.ndjson").read_text(encoding="utf-8").splitlines()[0])
    assert envelope["engrama_export"] == EXPORT_FORMAT_VERSION
    assert envelope["source_backend"] in {"sqlite", "neo4j"}
    assert "exported_at" in envelope
    assert envelope["embedding_dimensions"] == 0


def test_import_rejects_unsupported_format_version(target_store: SqliteGraphStore, tmp_path: Path):
    dump = tmp_path / "bad.ndjson"
    dump.write_text(json.dumps({"engrama_export": 999, "version": "?"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="export format v999"):
        import_graph(
            target_store,
            SqliteVecStore(target_store._conn, dimensions=0),  # type: ignore[attr-defined]
            dump,
        )


def test_import_rejects_empty_file(target_store: SqliteGraphStore, tmp_path: Path):
    empty = tmp_path / "empty.ndjson"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        import_graph(
            target_store,
            SqliteVecStore(target_store._conn, dimensions=0),  # type: ignore[attr-defined]
            empty,
        )


# ----------------------------------------------------------------------
# Round-trip
# ----------------------------------------------------------------------


def test_round_trip_preserves_nodes_and_relations(
    source_store: SqliteGraphStore, target_store: SqliteGraphStore, tmp_path: Path
):
    _seed(source_store)
    source_vec = SqliteVecStore(source_store._conn, dimensions=0)  # type: ignore[attr-defined]
    target_vec = SqliteVecStore(target_store._conn, dimensions=0)  # type: ignore[attr-defined]

    export_graph(source_store, source_vec, tmp_path / "dump.ndjson")
    counts = import_graph(target_store, target_vec, tmp_path / "dump.ndjson")

    assert counts["nodes"] == 3
    assert counts["relations"] == 2
    assert counts["vectors"] == 0
    assert counts["skipped_vectors"] == 0

    # Verify the target by sampling a node and walking its neighbours.
    node = target_store.get_node("Concept", "name", "Alpha")
    assert node is not None
    assert node.get("description") == "first"

    # Check the imported edge via iter_all_relations (shape is portable
    # across backends; get_neighbours returns a different shape per
    # backend so it's not the right tool for this assertion).
    rows = list(target_store.iter_all_relations())
    assert any(
        r["from_value"] == "Alpha" and r["to_value"] == "P1" and r["rel_type"] == "USES"
        for r in rows
    )


def test_round_trip_with_vectors_when_dimensions_match(
    source_store: SqliteGraphStore, target_store: SqliteGraphStore, tmp_path: Path
):
    _seed(source_store)
    source_vec = SqliteVecStore(source_store._conn, dimensions=4)  # type: ignore[attr-defined]
    source_vec.ensure_index()
    source_vec.store_vector_by_key("Concept", "name", "Alpha", [0.1, 0.2, 0.3, 0.4])
    source_vec.store_vector_by_key("Project", "name", "P1", [0.5, 0.6, 0.7, 0.8])

    export_graph(source_store, source_vec, tmp_path / "dump.ndjson")

    target_vec = SqliteVecStore(target_store._conn, dimensions=4)  # type: ignore[attr-defined]
    target_vec.ensure_index()
    counts = import_graph(target_store, target_vec, tmp_path / "dump.ndjson")

    assert counts["nodes"] == 3
    assert counts["vectors"] == 2
    assert counts["skipped_vectors"] == 0

    # Pull the imported vector back and check it matches.
    imported = {(v["label"], v["key_value"]): v["vector"] for v in target_vec.iter_all_vectors()}
    assert ("Concept", "Alpha") in imported
    assert imported[("Concept", "Alpha")] == pytest.approx([0.1, 0.2, 0.3, 0.4])


def test_vectors_skipped_when_dimensions_mismatch(
    source_store: SqliteGraphStore, target_store: SqliteGraphStore, tmp_path: Path
):
    _seed(source_store)
    source_vec = SqliteVecStore(source_store._conn, dimensions=4)  # type: ignore[attr-defined]
    source_vec.ensure_index()
    source_vec.store_vector_by_key("Concept", "name", "Alpha", [0.1, 0.2, 0.3, 0.4])

    export_graph(source_store, source_vec, tmp_path / "dump.ndjson")

    # Target with different dimensions
    target_vec = SqliteVecStore(target_store._conn, dimensions=8)  # type: ignore[attr-defined]
    target_vec.ensure_index()
    counts = import_graph(target_store, target_vec, tmp_path / "dump.ndjson")

    assert counts["vectors"] == 0
    assert counts["skipped_vectors"] == 1
    # The graph itself should still have imported cleanly.
    assert counts["nodes"] == 3


def test_no_vectors_flag_omits_vector_records(source_store: SqliteGraphStore, tmp_path: Path):
    _seed(source_store)
    source_vec = SqliteVecStore(source_store._conn, dimensions=4)  # type: ignore[attr-defined]
    source_vec.ensure_index()
    source_vec.store_vector_by_key("Concept", "name", "Alpha", [0.1, 0.2, 0.3, 0.4])

    counts = export_graph(source_store, source_vec, tmp_path / "dump.ndjson", with_vectors=False)
    assert counts["vectors"] == 0

    types = [
        json.loads(line)["type"]
        for line in (tmp_path / "dump.ndjson").read_text(encoding="utf-8").splitlines()[1:]
    ]
    assert "vector" not in types


# ----------------------------------------------------------------------
# Purge
# ----------------------------------------------------------------------


def test_purge_wipes_destination_before_import(
    source_store: SqliteGraphStore, target_store: SqliteGraphStore, tmp_path: Path
):
    _seed(source_store)

    # Pre-populate target with junk that should disappear on --purge.
    target_store.merge_node("Concept", "name", "ToBeWiped", {"description": "junk"})
    assert target_store.get_node("Concept", "name", "ToBeWiped") is not None

    source_vec = SqliteVecStore(source_store._conn, dimensions=0)  # type: ignore[attr-defined]
    target_vec = SqliteVecStore(target_store._conn, dimensions=0)  # type: ignore[attr-defined]

    export_graph(source_store, source_vec, tmp_path / "dump.ndjson")
    import_graph(target_store, target_vec, tmp_path / "dump.ndjson", purge=True)

    assert target_store.get_node("Concept", "name", "ToBeWiped") is None
    assert target_store.get_node("Concept", "name", "Alpha") is not None


def test_import_is_additive_without_purge(
    source_store: SqliteGraphStore, target_store: SqliteGraphStore, tmp_path: Path
):
    _seed(source_store)

    target_store.merge_node("Concept", "name", "PreExisting", {"description": "kept"})

    source_vec = SqliteVecStore(source_store._conn, dimensions=0)  # type: ignore[attr-defined]
    target_vec = SqliteVecStore(target_store._conn, dimensions=0)  # type: ignore[attr-defined]

    export_graph(source_store, source_vec, tmp_path / "dump.ndjson")
    import_graph(target_store, target_vec, tmp_path / "dump.ndjson", purge=False)

    assert target_store.get_node("Concept", "name", "PreExisting") is not None
    assert target_store.get_node("Concept", "name", "Alpha") is not None


# ----------------------------------------------------------------------
# Forward compatibility
# ----------------------------------------------------------------------


def test_unknown_record_types_are_silently_ignored(target_store: SqliteGraphStore, tmp_path: Path):
    """An older engrama reading a newer dump should skip records it
    doesn't understand instead of crashing."""
    dump = tmp_path / "future.ndjson"
    envelope = {
        "engrama_export": EXPORT_FORMAT_VERSION,
        "version": "future",
        "embedding_dimensions": 0,
    }
    node_rec = {"type": "node", "label": "Concept", "key_field": "name", "key_value": "X"}
    future_rec = {"type": "future_record_type", "payload": {"anything": True}}
    dump.write_text(
        json.dumps(envelope) + "\n" + json.dumps(node_rec) + "\n" + json.dumps(future_rec) + "\n",
        encoding="utf-8",
    )
    target_vec = SqliteVecStore(target_store._conn, dimensions=0)  # type: ignore[attr-defined]
    counts = import_graph(target_store, target_vec, dump)
    assert counts["nodes"] == 1
    assert target_store.get_node("Concept", "name", "X") is not None
