"""Read-time recency (DDR-007): activity, per-kind half-lives, degree factor."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engrama.backends.sqlite.store import SqliteGraphStore
from engrama.core.scope import MemoryScope
from engrama.core.temporal import recency

OWNER = MemoryScope(org_id="rec-org", user_id="rec-user")


def _ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


KW = {"half_life": 180.0, "insight_half_life": 30.0}


def test_anchor_labels_do_not_fade() -> None:
    assert recency("Project", {"updated_at": _ago(900)}, **KW) == 1.0


def test_half_life_depends_on_the_kind_of_node() -> None:
    domain = recency("Concept", {"updated_at": _ago(180)}, **KW)
    system = recency("Insight", {"updated_at": _ago(30), "source_query": "q"}, **KW)
    hand = recency("Insight", {"updated_at": _ago(180)}, **KW)
    assert domain == pytest.approx(0.5, abs=0.01)
    assert system == pytest.approx(0.5, abs=0.01)
    assert hand == pytest.approx(0.5, abs=0.01)  # hand-written Insight = domain node


def test_hubs_fade_slower_and_activity_beats_updated_at() -> None:
    lone = recency("Concept", {"updated_at": _ago(360)}, **KW)
    hub = recency("Concept", {"updated_at": _ago(360), "degree": 7}, **KW)
    assert hub > lone
    assert recency(
        "Concept", {"updated_at": _ago(360), "degree": 7}, degree_factor=False, **KW
    ) == pytest.approx(lone)
    linked = recency("Concept", {"updated_at": _ago(360), "last_activity_at": _ago(1)}, **KW)
    assert linked > 0.99
    assert recency("Concept", {}, **KW) is None


@pytest.fixture()
def store(tmp_path: Path):
    s = SqliteGraphStore(tmp_path / "r.db")
    yield s
    s.close()


def _props(store: SqliteGraphStore, name: str) -> tuple[dict, str]:
    row = store._conn.execute(
        "SELECT props, updated_at FROM nodes WHERE key_value = ?", (name,)
    ).fetchone()
    return json.loads(row["props"]), row["updated_at"]


def test_linking_is_activity_on_both_endpoints(store: SqliteGraphStore) -> None:
    store.merge_node("Concept", "name", "a", OWNER.to_properties())
    store.merge_node("Concept", "name", "b", OWNER.to_properties())
    old = _ago(100)
    store._conn.execute(
        "UPDATE nodes SET updated_at = ?, props = json_set(props, '$.last_activity_at', ?)",
        (old, old),
    )
    store._conn.commit()
    store.merge_relation("Concept", "name", "a", "RELATED_TO", "Concept", "name", "b", scope=OWNER)
    for name in ("a", "b"):
        props, updated_at = _props(store, name)
        assert updated_at == old  # linking is not an edit
        assert props["last_activity_at"] > old


def test_caller_cannot_set_activity(store: SqliteGraphStore) -> None:
    store.merge_node(
        "Concept", "name", "a", {"last_activity_at": _ago(900), **OWNER.to_properties()}
    )
    assert _props(store, "a")[0]["last_activity_at"] > _ago(1)


def test_fulltext_results_carry_recency_inputs(store: SqliteGraphStore) -> None:
    store.merge_node("Concept", "name", "graph hub", {"summary": "graph", **OWNER.to_properties()})
    for i in range(3):
        store.merge_node("Concept", "name", f"leaf{i}", OWNER.to_properties())
        store.merge_relation(
            "Concept", "name", f"leaf{i}", "RELATED_TO", "Concept", "name", "graph hub", scope=OWNER
        )
    hit = next(r for r in store.fulltext_search("graph", scope=OWNER) if r["name"] == "graph hub")
    assert hit["degree"] == 3 and hit["last_activity_at"]


def test_import_keeps_activity(tmp_path: Path) -> None:
    from engrama.backends.sqlite.vector import SqliteVecStore
    from engrama.migrate import export_graph, import_graph

    src = SqliteGraphStore(tmp_path / "src.db")
    src.merge_node("Concept", "name", "a", OWNER.to_properties())
    old = _ago(50)
    src._conn.execute("UPDATE nodes SET props = json_set(props, '$.last_activity_at', ?)", (old,))
    src._conn.commit()
    dump = tmp_path / "d.ndjson"
    export_graph(src, SqliteVecStore(src._conn, dimensions=0), dump)
    src.close()
    dst = SqliteGraphStore(tmp_path / "dst.db")
    try:
        import_graph(dst, SqliteVecStore(dst._conn, dimensions=0), dump)
        assert _props(dst, "a")[0]["last_activity_at"] == old
    finally:
        dst.close()


async def test_status_reports_recency(tmp_path: Path, monkeypatch) -> None:
    from mcp.client import Client

    from engrama.adapters.mcp.server import create_engrama_mcp

    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    monkeypatch.setenv("ENGRAMA_RECENCY_HALF_LIFE", "90")
    server = create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(tmp_path / "s.db")}
    )
    async with Client(server) as client:
        result = await client.call_tool("engrama_status", {})
    rec = json.loads(result.content[0].text)["search"]["recency"]  # type: ignore[union-attr]
    assert rec["half_life_days"] == 90.0 and rec["insight_half_life_days"] == 30.0
    assert "Project" in rec["exempt_labels"]


@pytest.mark.neo4j
def test_neo4j_sync_linking_is_activity() -> None:
    from engrama.backends.neo4j.backend import Neo4jGraphStore
    from engrama.core.client import EngramaClient

    p = f"ra{uuid.uuid4().hex[:6]}-"
    client = EngramaClient()
    s = Neo4jGraphStore(client)
    try:
        for n in ("a", "b"):
            s.merge_node("Concept", "name", f"{p}{n}", OWNER.to_properties())
        client.run(
            "MATCH (n) WHERE n.name STARTS WITH $p "
            "SET n.last_activity_at = datetime() - duration({days: 100}), "
            "n.updated_at = datetime() - duration({days: 100})",
            {"p": p},
        )
        s.merge_relation(
            "Concept", "name", f"{p}a", "RELATED_TO", "Concept", "name", f"{p}b", scope=OWNER
        )
        rows = client.run(
            "MATCH (n) WHERE n.name STARTS WITH $p RETURN "
            "duration.inDays(n.last_activity_at, datetime()).days AS act, "
            "duration.inDays(n.updated_at, datetime()).days AS upd",
            {"p": p},
        )
        assert all(r["act"] == 0 and r["upd"] >= 99 for r in rows) and len(rows) == 2
        hit = next(r for r in s.fulltext_search(f"{p}a", scope=OWNER) if r["name"] == f"{p}a")
        assert hit["degree"] == 1 and hit["last_activity_at"]
    finally:
        client.run("MATCH (n) WHERE n.name STARTS WITH $p DETACH DELETE n", {"p": p})
        client.close()


@pytest.mark.neo4j
async def test_neo4j_async_linking_is_activity() -> None:
    from engrama.backends import create_async_stores

    p = f"rb{uuid.uuid4().hex[:6]}-"
    s, _ = create_async_stores({"GRAPH_BACKEND": "neo4j"})
    try:
        for n in ("a", "b"):
            await s.merge_node("Concept", "name", f"{p}{n}", OWNER.to_properties())
        await s._driver.execute_query(
            "MATCH (n) WHERE n.name STARTS WITH $p "
            "SET n.last_activity_at = datetime() - duration({days: 100})",
            parameters_={"p": p},
        )
        await s.merge_relation(
            "Concept", "name", f"{p}a", "RELATED_TO", "Concept", "name", f"{p}b", scope=OWNER
        )
        records, _, _ = await s._driver.execute_query(
            "MATCH (n) WHERE n.name STARTS WITH $p "
            "RETURN duration.inDays(n.last_activity_at, datetime()).days AS act",
            parameters_={"p": p},
        )
        assert [r["act"] for r in records] == [0, 0]
    finally:
        await s._driver.execute_query(
            "MATCH (n) WHERE n.name STARTS WITH $p DETACH DELETE n", parameters_={"p": p}
        )
        await s.close()
