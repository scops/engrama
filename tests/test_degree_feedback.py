"""Degree feedback and tag anchoring on write (DDR-006 items 4-5)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from engrama.backends.sqlite.store import SqliteGraphStore
from engrama.core.anchors import suggest_from_tags
from engrama.core.scope import MemoryScope

OWNER = MemoryScope(org_id="deg-org", user_id="deg-user")

ANCHORS = [
    {"label": "Project", "name": "Atlas"},
    {"label": "Course", "name": "Graph Basics"},
    {"label": "Domain", "name": "data-science"},
]


def test_suggestions_normalise_and_skip_linked_and_self() -> None:
    out = suggest_from_tags(
        "Concept",
        "note",
        ["atlas", "graph-basics", "Data Science", "unrelated", "ATLAS"],
        ANCHORS,
        linked={("Domain", "data-science")},
    )
    assert out == [
        {
            "label": "Project",
            "name": "Atlas",
            "tag": "atlas",
            "rel_type": "BELONGS_TO",
            "direction": "out",
        },
        {
            "label": "Course",
            "name": "Graph Basics",
            "tag": "graph-basics",
            "rel_type": "COVERS",
            "direction": "in",
        },
    ]
    assert suggest_from_tags("Project", "Atlas", ["atlas"], ANCHORS, set()) == []


# --- MCP write path ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in (
        "ENGRAMA_ORG_ID",
        "ENGRAMA_USER_ID",
        "ENGRAMA_LOCAL_SUB",
        "VAULT_PATH",
        "ENGRAMA_TAG_LINKING",
        "ENGRAMA_REQUIRE_RELATIONS",
    ):
        monkeypatch.delenv(var, raising=False)


def _seed(db: Path) -> None:
    s = SqliteGraphStore(db)
    for a in ANCHORS:
        key = "name"
        s.merge_node(a["label"], key, a["name"], OWNER.to_properties())
    s.close()


async def _remember(
    db: Path, monkeypatch, properties: dict, label: str = "Concept", relations=None
):
    from mcp.client import Client

    from engrama.adapters.mcp import server as srv_module
    from engrama.adapters.mcp.server import create_engrama_mcp

    server = create_engrama_mcp(backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)})
    monkeypatch.setattr(srv_module, "resolve_scope", lambda _ctx: OWNER)
    params = {"label": label, "properties": properties}
    if relations:
        params["relations"] = relations
    async with Client(server) as client:
        result = await client.call_tool("engrama_remember", {"params": params})
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


def _edges(db: Path) -> set[tuple[str, str, str]]:
    s = SqliteGraphStore(db)
    try:
        rows = s._conn.execute(
            "SELECT f.key_value, e.rel_type, t.key_value FROM edges e "
            "JOIN nodes f ON f.id = e.from_id JOIN nodes t ON t.id = e.to_id"
        ).fetchall()
        return {tuple(r) for r in rows}
    finally:
        s.close()


async def test_orphan_write_reports_degree_and_warning(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "a.db"
    _seed(db)
    out = await _remember(db, monkeypatch, {"name": "loose idea", "summary": "x"})
    assert out["status"] == "ok"
    assert out["degree"] == 0
    assert "orphan_warning" in out
    assert "suggested_relations" not in out


async def test_tags_become_suggestions_by_default(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "b.db"
    _seed(db)
    out = await _remember(db, monkeypatch, {"name": "note", "tags": ["atlas", "graph-basics"]})
    assert {(s["label"], s["rel_type"], s["direction"]) for s in out["suggested_relations"]} == {
        ("Project", "BELONGS_TO", "out"),
        ("Course", "COVERS", "in"),
    }
    assert out["degree"] == 0
    assert _edges(db) == set()  # suggest never writes


async def test_auto_mode_creates_the_edges(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ENGRAMA_TAG_LINKING", "auto")
    db = tmp_path / "c.db"
    _seed(db)
    out = await _remember(db, monkeypatch, {"name": "note", "tags": ["atlas", "graph-basics"]})
    assert out["degree"] == 2
    assert "orphan_warning" not in out
    assert len(out["relations_from_tags"]) == 2
    assert _edges(db) == {
        ("note", "BELONGS_TO", "Atlas"),
        ("Graph Basics", "COVERS", "note"),
    }


async def test_off_mode_ignores_tags(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ENGRAMA_TAG_LINKING", "off")
    db = tmp_path / "d.db"
    _seed(db)
    out = await _remember(db, monkeypatch, {"name": "note", "tags": ["atlas"]})
    assert "suggested_relations" not in out


async def test_require_relations_refuses_an_unlinked_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ENGRAMA_REQUIRE_RELATIONS", "true")
    db = tmp_path / "e.db"
    _seed(db)
    refused = await _remember(db, monkeypatch, {"name": "floating", "tags": ["atlas"]})
    assert refused["status"] == "error"
    assert refused["suggested_relations"][0]["name"] == "Atlas"
    s = SqliteGraphStore(db)
    assert s.get_node("Concept", "name", "floating", scope=OWNER) is None
    s.close()
    # With a relation, or as an anchor label, the write goes through.
    ok = await _remember(
        db,
        monkeypatch,
        {"name": "linked"},
        relations={"BELONGS_TO": [{"name": "Atlas", "label": "Project"}]},
    )
    assert ok["status"] == "ok" and ok["degree"] == 1
    anchor = await _remember(db, monkeypatch, {"name": "New Project"}, label="Project")
    assert anchor["status"] == "ok"


async def test_mcp_reflect_reports_unlinked_tags_and_hub_stubs(tmp_path: Path, monkeypatch) -> None:
    from mcp.client import Client

    from engrama.adapters.mcp import server as srv_module
    from engrama.adapters.mcp.server import create_engrama_mcp

    db = tmp_path / "f.db"
    s = SqliteGraphStore(db)
    s.merge_node("Project", "name", "Atlas", OWNER.to_properties())
    s.merge_node("Technology", "name", "hub", {"status": "stub", **OWNER.to_properties()})
    for i in range(3):
        s.merge_node("Concept", "name", f"c{i}", {"tags": ["atlas"], **OWNER.to_properties()})
        s.merge_relation(
            "Concept", "name", f"c{i}", "USES", "Technology", "name", "hub", scope=OWNER
        )
    s.close()

    server = create_engrama_mcp(backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)})
    monkeypatch.setattr(srv_module, "resolve_scope", lambda _ctx: OWNER)
    async with Client(server) as client:
        result = await client.call_tool("engrama_reflect", {})
    titles = {i["title"] for i in json.loads(result.content[0].text)["insights"]}  # type: ignore[union-attr]
    assert "Unlinked tag: Project:Atlas" in titles
    assert "Hub stub: Technology:hub" in titles


# --- Neo4j stores -----------------------------------------------------------


def _seed_tagged(merge_node, p: str) -> None:
    merge_node("Project", "name", f"{p}Atlas", OWNER.to_properties())
    merge_node("Project", "name", f"{p}Old", {"status": "archived", **OWNER.to_properties()})
    for i in range(2):
        merge_node("Concept", "name", f"{p}c{i}", {"tags": [f"{p}atlas"], **OWNER.to_properties()})


@pytest.mark.neo4j
def test_neo4j_sync_anchors_and_tag_detector() -> None:
    from engrama.backends.neo4j.backend import Neo4jGraphStore
    from engrama.core.client import EngramaClient

    p = f"dg{uuid.uuid4().hex[:6]}-"
    client = EngramaClient()
    s = Neo4jGraphStore(client)
    try:
        _seed_tagged(s.merge_node, p)
        anchors = {(a["label"], a["name"]) for a in s.list_anchors(scope=OWNER)}
        assert ("Project", f"{p}Atlas") in anchors and ("Project", f"{p}Old") not in anchors
        rows = [r for r in s.detect_tags_without_edge(scope=OWNER) if r["name"] == f"{p}Atlas"]
        assert [len(r["nodes"]) for r in rows] == [2]
    finally:
        client.run(
            "MATCH (n) WHERE coalesce(n.name, n.title) STARTS WITH $p DETACH DELETE n", {"p": p}
        )
        client.close()


@pytest.mark.neo4j
async def test_neo4j_async_anchors_and_tag_detector() -> None:
    from engrama.backends import create_async_stores

    p = f"da{uuid.uuid4().hex[:6]}-"
    s, _ = create_async_stores({"GRAPH_BACKEND": "neo4j"})
    calls: list = []
    try:
        _seed_tagged(lambda *a: calls.append(a), p)
        for c in calls:
            await s.merge_node(*c)
        anchors = {(a["label"], a["name"]) for a in await s.list_anchors(scope=OWNER)}
        assert ("Project", f"{p}Atlas") in anchors
        rows = [
            r for r in await s.detect_tags_without_edge(scope=OWNER) if r["name"] == f"{p}Atlas"
        ]
        assert [len(r["nodes"]) for r in rows] == [2]
    finally:
        await s._driver.execute_query(
            "MATCH (n) WHERE coalesce(n.name, n.title) STARTS WITH $p DETACH DELETE n",
            parameters_={"p": p},
        )
        await s.close()
