"""Stub lifecycle (DDR-006 item 6) and hub stubs (DDR-008)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from engrama.backends.sqlite.store import SqliteGraphStore
from engrama.core.scope import MemoryScope

OWNER = MemoryScope(org_id="stub-org", user_id="stub-user")
OTHER = MemoryScope(org_id="other-org", user_id="other-user")


def _props(**extra) -> dict:
    return {**extra, **OWNER.to_properties()}


@pytest.fixture()
def store(tmp_path: Path):
    s = SqliteGraphStore(tmp_path / "stubs.db")
    yield s
    s.close()


def _status(store: SqliteGraphStore, name: str) -> str | None:
    node = store.get_node("Technology", "name", name, scope=OWNER)
    return node.get("status") if node else None


@pytest.mark.parametrize(
    ("write", "expected"),
    [
        ({"summary": "now it has content"}, "active"),
        ({"details": "long form"}, "active"),
        ({"summary": "content", "status": "deprecated"}, "deprecated"),
        ({"tags": ["x"]}, "stub"),  # no content → still a stub
    ],
)
def test_enriching_a_stub_promotes_it(store: SqliteGraphStore, write: dict, expected: str) -> None:
    store.merge_node("Technology", "name", "tool", _props(status="stub"))
    store.merge_node("Technology", "name", "tool", _props(**write))
    assert _status(store, "tool") == expected


def test_non_stub_status_is_left_alone(store: SqliteGraphStore) -> None:
    store.merge_node("Technology", "name", "tool", _props(status="superseded"))
    store.merge_node("Technology", "name", "tool", _props(summary="more"))
    assert _status(store, "tool") == "superseded"


def _seed_hub(merge_node, merge_relation, p: str = "", owner: MemoryScope = OWNER) -> None:
    merge_node("Technology", "name", f"{p}hub", {"status": "stub", **owner.to_properties()})
    merge_node("Technology", "name", f"{p}small", {"status": "stub", **owner.to_properties()})
    for i in range(3):
        merge_node("Project", "name", f"{p}p{i}", owner.to_properties())
        merge_relation(
            "Project", "name", f"{p}p{i}", "USES", "Technology", "name", f"{p}hub", scope=owner
        )
    for i in range(2):
        merge_relation(
            "Project", "name", f"{p}p{i}", "USES", "Technology", "name", f"{p}small", scope=owner
        )
    # An annotation edge from a reflect Insight doesn't make "small" a hub.
    merge_node(
        "Insight",
        "title",
        f"{p}note",
        {"source_query": "test", "status": "pending", **owner.to_properties()},
    )
    merge_relation(
        "Insight", "title", f"{p}note", "ABOUT", "Technology", "name", f"{p}small", scope=owner
    )


def test_node_degree_is_scoped_and_ignores_annotations(store: SqliteGraphStore) -> None:
    _seed_hub(store.merge_node, store.merge_relation)
    assert store.node_degree("Technology", "name", "hub", scope=OWNER) == 3
    assert store.node_degree("Technology", "name", "small", scope=OWNER) == 2
    assert store.node_degree("Technology", "name", "hub", scope=OTHER) is None
    assert store.node_degree("Technology", "name", "hub") is None  # fail-closed


def test_sqlite_hub_stub_detector(store: SqliteGraphStore) -> None:
    _seed_hub(store.merge_node, store.merge_relation)
    rows = store.detect_hub_stubs(scope=OWNER)
    assert [(r["name"], r["degree"]) for r in rows] == [("hub", 3)]
    assert store.detect_hub_stubs(scope=OTHER) == []


@pytest.mark.neo4j
def test_neo4j_sync_stub_lifecycle_and_detector() -> None:
    from engrama.backends.neo4j.backend import Neo4jGraphStore
    from engrama.core.client import EngramaClient

    p = f"st{uuid.uuid4().hex[:6]}-"
    client = EngramaClient()
    s = Neo4jGraphStore(client)
    try:
        _seed_hub(s.merge_node, s.merge_relation, p)
        assert [(r["name"], r["degree"]) for r in s.detect_hub_stubs(scope=OWNER)] == [
            (f"{p}hub", 3)
        ]
        assert s.node_degree("Technology", "name", f"{p}small", scope=OWNER) == 2
        s.merge_node("Technology", "name", f"{p}hub", _props(summary="explained"))
        assert s.get_node("Technology", "name", f"{p}hub", scope=OWNER)["status"] == "active"
    finally:
        client.run(
            "MATCH (n) WHERE coalesce(n.name, n.title) STARTS WITH $p DETACH DELETE n", {"p": p}
        )
        client.close()


@pytest.mark.neo4j
async def test_neo4j_async_stub_lifecycle_and_detector() -> None:
    from engrama.backends import create_async_stores

    p = f"sa{uuid.uuid4().hex[:6]}-"
    s, _ = create_async_stores({"GRAPH_BACKEND": "neo4j"})
    calls: list = []
    try:
        _seed_hub(lambda *a: calls.append(("n", a)), lambda *a, **k: calls.append(("r", a, k)), p)
        for c in calls:
            if c[0] == "n":
                await s.merge_node(*c[1])
            else:
                await s.merge_relation(*c[1], **c[2])
        rows = await s.detect_hub_stubs(scope=OWNER)
        assert [(r["name"], r["degree"]) for r in rows] == [(f"{p}hub", 3)]
        await s.merge_node("Technology", "name", f"{p}hub", _props(details="explained"))
        assert (await s.get_node("Technology", "name", f"{p}hub", scope=OWNER))[
            "status"
        ] == "active"
    finally:
        await s._driver.execute_query(
            "MATCH (n) WHERE coalesce(n.name, n.title) STARTS WITH $p DETACH DELETE n",
            parameters_={"p": p},
        )
        await s.close()


@pytest.fixture()
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in ("ENGRAMA_ORG_ID", "ENGRAMA_USER_ID", "ENGRAMA_LOCAL_SUB", "VAULT_PATH"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.usefixtures("_hermetic_env")
def test_reflect_proposes_enriching_hub_stubs(tmp_path: Path) -> None:
    from engrama import Engrama

    with Engrama(
        backend="sqlite", db_path=tmp_path / "r.db", org_id=OWNER.org_id, user_id=OWNER.user_id
    ) as eng:
        _seed_hub(eng._store.merge_node, eng._store.merge_relation)
        titles = {i.title for i in eng.reflect()}
    assert "Hub stub: Technology:hub" in titles
    assert "Hub stub: Technology:small" not in titles


@pytest.mark.usefixtures("_hermetic_env")
async def test_mcp_remember_hints_at_hub_stubs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.client import Client

    from engrama.adapters.mcp import server as srv_module
    from engrama.adapters.mcp.server import create_engrama_mcp

    db = tmp_path / "mcp.db"
    seed = SqliteGraphStore(db)
    _seed_hub(seed.merge_node, seed.merge_relation)
    seed.close()

    server = create_engrama_mcp(backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)})
    monkeypatch.setattr(srv_module, "resolve_scope", lambda _ctx: OWNER)
    async with Client(server) as client:
        result = await client.call_tool(
            "engrama_remember",
            {
                "params": {
                    "label": "Project",
                    "properties": {"name": "new-project", "summary": "uses the hub"},
                    "relations": {"USES": [{"name": "hub", "label": "Technology"}, "small"]},
                }
            },
        )
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    # "small" crosses the threshold with this very edge (2 → 3).
    assert sorted(payload["enrich_hints"], key=lambda h: h["name"]) == [
        {"label": "Technology", "name": "hub", "degree": 4},
        {"label": "Technology", "name": "small", "degree": 3},
    ]
    assert "enrich_hints_note" in payload
