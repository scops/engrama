"""Relevance over time without stored decay (DDR-007).

Stored confidence only changes on an explicit statement, archiving is not
activity, ``engrama decay`` no longer writes, and an import keeps the graph's
timestamps.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

import pytest

from engrama.backends.sqlite.store import SqliteGraphStore
from engrama.backends.sqlite.vector import SqliteVecStore
from engrama.core.scope import MemoryScope

OWNER = MemoryScope(org_id="recency", user_id="recency")
PAST = "2026-01-02T03:04:05+00:00"


@pytest.fixture()
def store(tmp_path: Path):
    s = SqliteGraphStore(tmp_path / "recency.db")
    yield s
    s.close()


def _row(store: SqliteGraphStore, name: str) -> tuple[dict, str, str]:
    r = store._conn.execute(
        "SELECT props, created_at, updated_at FROM nodes WHERE key_value = ?", (name,)
    ).fetchone()
    return json.loads(r["props"]), r["created_at"], r["updated_at"]


def _backdate(store: SqliteGraphStore, name: str) -> None:
    store._conn.execute(
        "UPDATE nodes SET updated_at = ?, created_at = ? WHERE key_value = ?", (PAST, PAST, name)
    )
    store._conn.commit()


def test_decay_cli_is_a_noop(store: SqliteGraphStore, capsys) -> None:
    from engrama.cli import cmd_decay

    args = argparse.Namespace(rate=0.5, min_confidence=0.9, max_age=1, label=None, dry_run=False)
    assert cmd_decay(args) == 0
    assert "deprecated" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("archive", "reason"),
    [
        (lambda s: s.archive_node_by_name("Concept", "c", owner=OWNER), "forget"),
        (lambda s: s.delete_node("Concept", "name", "c"), "delete"),
        (lambda s: s.archive_nodes_older_than("Concept", days=1), "ttl"),
        (lambda s: s.archive_node_for_missing_note("Concept", "c"), "missing_note"),
    ],
)
def test_archiving_is_not_activity(store: SqliteGraphStore, archive, reason: str) -> None:
    store.merge_node("Concept", "name", "c", OWNER.to_properties())
    _backdate(store, "c")
    archive(store)
    props, _, updated_at = _row(store, "c")
    assert (props["status"], props["archived_reason"]) == ("archived", reason)
    assert props["archived_at"]
    assert updated_at == PAST


def test_explicit_confidence_applies_on_update(store: SqliteGraphStore) -> None:
    store.merge_node("Insight", "title", "t", {"confidence": 0.03, **OWNER.to_properties()})
    store.merge_node("Insight", "title", "t", {"confidence": 0.4, **OWNER.to_properties()})
    assert _row(store, "t")[0]["confidence"] == 0.4
    # Absent on update → the stored value is left alone.
    store.merge_node("Insight", "title", "t", {"body": "x", **OWNER.to_properties()})
    assert _row(store, "t")[0]["confidence"] == 0.4


def test_import_keeps_timestamps(tmp_path: Path) -> None:
    from engrama.migrate import export_graph, import_graph

    src = SqliteGraphStore(tmp_path / "src.db")
    src.merge_node("Concept", "name", "old", OWNER.to_properties())
    _backdate(src, "old")
    dump = tmp_path / "dump.ndjson"
    export_graph(src, SqliteVecStore(src._conn, dimensions=0), dump)
    src.close()

    dst = SqliteGraphStore(tmp_path / "dst.db")
    try:
        import_graph(dst, SqliteVecStore(dst._conn, dimensions=0), dump)
        props, created_at, updated_at = _row(dst, "old")
        assert (created_at, updated_at) == (PAST, PAST)
        assert "created_at" not in props and "updated_at" not in props
    finally:
        dst.close()


@pytest.mark.neo4j
def test_neo4j_confidence_and_archive_semantics() -> None:
    from engrama.backends.neo4j.backend import Neo4jGraphStore
    from engrama.core.client import EngramaClient

    owner = MemoryScope(org_id=f"rec-{uuid.uuid4().hex[:8]}", user_id="u")
    client = EngramaClient()
    store = Neo4jGraphStore(client)
    try:
        store.merge_node("Insight", "title", "t", {"confidence": 0.03, **owner.to_properties()})
        store.merge_node("Insight", "title", "t", {"confidence": 0.4, **owner.to_properties()})
        store.merge_node("Concept", "name", "c", owner.to_properties())
        client.run(
            "MATCH (n:Concept {name: 'c', org_id: $o}) SET n.updated_at = datetime($p)",
            {"o": owner.org_id, "p": PAST},
        )
        store.archive_node_by_name("Concept", "c", owner=owner)
        rows = client.run(
            "MATCH (n) WHERE n.org_id = $o RETURN coalesce(n.name, n.title) AS k, "
            "n.confidence AS conf, n.archived_reason AS reason, toString(n.updated_at) AS upd",
            {"o": owner.org_id},
        )
        by_key = {r["k"]: r for r in rows}
        assert by_key["t"]["conf"] == 0.4
        assert by_key["c"]["reason"] == "forget"
        assert by_key["c"]["upd"].startswith("2026-01-02T03:04:05")
    finally:
        client.run("MATCH (n) WHERE n.org_id = $o DETACH DELETE n", {"o": owner.org_id})
        client.close()


@pytest.mark.neo4j
async def test_neo4j_async_confidence_applies_on_update() -> None:
    from engrama.backends import create_async_stores

    owner = MemoryScope(org_id=f"rec-{uuid.uuid4().hex[:8]}", user_id="u")
    store, _ = create_async_stores({"GRAPH_BACKEND": "neo4j"})
    try:
        await store.merge_node(
            "Insight", "title", "t", {"confidence": 0.03, **owner.to_properties()}
        )
        await store.merge_node(
            "Insight", "title", "t", {"confidence": 0.4, **owner.to_properties()}
        )
        node = await store.get_node("Insight", "title", "t", scope=owner)
        assert node["confidence"] == 0.4
    finally:
        await store._driver.execute_query(
            "MATCH (n) WHERE n.org_id = $o DETACH DELETE n", parameters_={"o": owner.org_id}
        )
        await store.close()


@pytest.mark.neo4j
def test_import_into_neo4j_keeps_timestamps(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from engrama.backends.neo4j.backend import Neo4jGraphStore
    from engrama.core.client import EngramaClient
    from engrama.migrate import export_graph, import_graph

    owner = MemoryScope(org_id=f"rec-{uuid.uuid4().hex[:8]}", user_id="u")
    src = SqliteGraphStore(tmp_path / "src.db")
    src.merge_node("Concept", "name", "old", owner.to_properties())
    _backdate(src, "old")
    dump = tmp_path / "dump.ndjson"
    export_graph(src, SqliteVecStore(src._conn, dimensions=0), dump)
    src.close()

    client = EngramaClient()
    try:
        import_graph(Neo4jGraphStore(client), SimpleNamespace(dimensions=0), dump)
        rows = client.run(
            "MATCH (n:Concept {name: 'old', org_id: $o}) "
            "RETURN toString(n.created_at) AS c, toString(n.updated_at) AS u",
            {"o": owner.org_id},
        )
        assert rows[0]["c"].startswith("2026-01-02T03:04:05")
        assert rows[0]["u"].startswith("2026-01-02T03:04:05")
    finally:
        client.run("MATCH (n) WHERE n.org_id = $o DETACH DELETE n", {"o": owner.org_id})
        client.close()
