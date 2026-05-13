"""
Cross-backend migration tests: SQLite → Neo4j and Neo4j → SQLite.

These run in the ``test-neo4j`` CI job because they need a live Neo4j
service. The ``neo4j_session`` fixture from ``conftest.py`` skips them
cleanly when ``NEO4J_PASSWORD`` is unset.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from engrama.backends.neo4j.backend import Neo4jGraphStore
from engrama.backends.neo4j.vector import Neo4jVectorStore
from engrama.backends.sqlite import SqliteGraphStore
from engrama.backends.sqlite.vector import SqliteVecStore
from engrama.core.client import EngramaClient
from engrama.migrate import export_graph, import_graph


def _seed_sqlite(store: SqliteGraphStore) -> None:
    store.merge_node("Concept", "name", "MigrationAlpha", {"description": "first"})
    store.merge_node("Concept", "name", "MigrationBeta", {"description": "second"})
    store.merge_node("Project", "name", "MigrationP1", {"status": "active"})
    store.merge_relation(
        "Concept", "name", "MigrationAlpha", "USES", "Project", "name", "MigrationP1"
    )
    store.merge_relation(
        "Concept", "name", "MigrationBeta", "USES", "Project", "name", "MigrationP1"
    )


@pytest.fixture()
def neo4j_clean(neo4j_session):
    """Drop any leftover migration test nodes and yield a Neo4j store."""
    neo4j_session.run("MATCH (n) WHERE n.name STARTS WITH 'Migration' DETACH DELETE n")
    client = EngramaClient(
        uri=os.environ["NEO4J_URI"],
        user=os.environ["NEO4J_USERNAME"],
        password=os.environ["NEO4J_PASSWORD"],
    )
    store = Neo4jGraphStore(client)
    vec = Neo4jVectorStore(client, dimensions=4)
    yield store, vec
    # Cleanup
    neo4j_session.run("MATCH (n) WHERE n.name STARTS WITH 'Migration' DETACH DELETE n")
    client.close()


def test_sqlite_to_neo4j_round_trip(tmp_path: Path, neo4j_clean) -> None:
    """A SQLite-side dump must restore cleanly into a real Neo4j."""
    target_graph, target_vec = neo4j_clean

    source = SqliteGraphStore(tmp_path / "source.db")
    try:
        _seed_sqlite(source)
        source_vec = SqliteVecStore(source._conn, dimensions=0)  # type: ignore[attr-defined]
        export_graph(source, source_vec, tmp_path / "dump.ndjson")
    finally:
        source.close()

    counts = import_graph(target_graph, target_vec, tmp_path / "dump.ndjson")

    assert counts["nodes"] == 3
    assert counts["relations"] == 2

    # Verify by walking the imported graph through the Neo4j store API.
    node = target_graph.get_node("Concept", "name", "MigrationAlpha")
    assert node is not None

    relations = list(target_graph.iter_all_relations())
    seen = {(r["from_value"], r["rel_type"], r["to_value"]) for r in relations}
    assert ("MigrationAlpha", "USES", "MigrationP1") in seen
    assert ("MigrationBeta", "USES", "MigrationP1") in seen


def test_neo4j_to_sqlite_round_trip(tmp_path: Path, neo4j_clean) -> None:
    """And the inverse direction: dump from Neo4j, restore into SQLite.

    The Neo4j instance the dev runs locally may already contain a lot
    of unrelated nodes, so we don't assert exact counts — just that
    every Migration* node and its edge round-tripped.
    """
    source_graph, source_vec = neo4j_clean
    source_graph.merge_node("Concept", "name", "MigrationAlpha", {"description": "first"})
    source_graph.merge_node("Concept", "name", "MigrationBeta", {"description": "second"})
    source_graph.merge_node("Project", "name", "MigrationP1", {"status": "active"})
    source_graph.merge_relation(
        "Concept", "name", "MigrationAlpha", "USES", "Project", "name", "MigrationP1"
    )

    export_graph(source_graph, source_vec, tmp_path / "dump.ndjson")

    target = SqliteGraphStore(tmp_path / "target.db")
    try:
        target_vec = SqliteVecStore(target._conn, dimensions=0)  # type: ignore[attr-defined]
        counts = import_graph(target, target_vec, tmp_path / "dump.ndjson")
        assert counts["nodes"] >= 3

        for name in ("MigrationAlpha", "MigrationBeta"):
            node = target.get_node("Concept", "name", name)
            assert node is not None, f"{name} missing from imported SQLite"

        node = target.get_node("Project", "name", "MigrationP1")
        assert node is not None

        rels = list(target.iter_all_relations())
        seen = {(r["from_value"], r["rel_type"], r["to_value"]) for r in rels}
        assert ("MigrationAlpha", "USES", "MigrationP1") in seen
    finally:
        target.close()
