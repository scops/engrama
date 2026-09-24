"""Entity resolution on write (DDR-006 items 1-3)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from engrama.backends.sqlite.store import SqliteGraphStore
from engrama.core.resolve import decide_target, name_fragments, possible_duplicates
from engrama.core.scope import MemoryScope

OWNER = MemoryScope(org_id="res-org", user_id="res-user")
OTHER = MemoryScope(org_id="other-org", user_id="other-user")


def _c(label: str, name: str, status: str | None = None) -> dict:
    return {"label": label, "name": name, "status": status}


# --- pure policy -----------------------------------------------------------------


def test_fragments_cover_typos_and_short_names() -> None:
    assert name_fragments("Graph-Store v2") == ["gra", "sto"]
    assert name_fragments("AI") == ["ai"]


@pytest.mark.parametrize(
    ("target", "explicit", "candidates", "kind", "reason"),
    [
        ("Atlas", None, [_c("Project", "atlas")], "connect", None),
        ("atlas service", None, [_c("Project", "Atlas-Service")], "connect", None),  # normalised
        ("Atlas", "Project", [_c("Client", "Atlas")], "ask", "label_conflict"),
        ("Atlas", None, [_c("Client", "Atlas"), _c("Project", "Atlas")], "ask", "label_conflict"),
        ("Atlas", None, [_c("Project", "Atlas", "archived")], "ask", "revive_candidate"),
        ("graph-stor", None, [_c("Tool", "graph-store")], "connect", None),  # typo, fuzzy
        (
            "graph-s",
            None,
            [_c("Tool", "graph-store"), _c("Tool", "graph-sync")],
            "ask",
            "ambiguous",
        ),
        ("zeta", None, [_c("Tool", "graph-store")], "create", None),
    ],
)
def test_decide_target(target, explicit, candidates, kind, reason) -> None:
    decision = decide_target(target, explicit, candidates)
    assert (decision.kind, decision.reason) == (kind, reason)


def test_same_label_exact_wins_over_other_labels() -> None:
    d = decide_target("Atlas", "Project", [_c("Client", "Atlas"), _c("Project", "Atlas")])
    assert (d.kind, d.label) == ("connect", "Project")


def test_possible_duplicates_reasons_and_self_exclusion() -> None:
    dups = possible_duplicates(
        "Concept",
        "graph store",
        [_c("Concept", "graph store"), _c("Tool", "Graph Store"), _c("Concept", "graph stores")],
        [{"label": "Concept", "name": "persistence layer", "cosine": 0.95}],
    )
    assert {(d["name"], d["reason"]) for d in dups} == {
        ("Graph Store", "same name, different label"),
        ("graph stores", "similar name"),
        ("persistence layer", "similar meaning"),
    }


# --- store candidates ---------------------------------------------------------------


def _seed(merge_node, p: str = "") -> None:
    for label, name, extra in (
        ("Tool", "graph-store", {}),
        ("Tool", "graph-sync", {}),
        ("Project", "100% coverage", {}),
        ("Project", "old-thing", {"status": "archived"}),
        ("Insight", "graph review", {"source_query": "test"}),
    ):
        key = "title" if label == "Insight" else "name"
        merge_node(label, key, f"{p}{name}", {**extra, **OWNER.to_properties()})


def test_sqlite_name_candidates(tmp_path: Path) -> None:
    s = SqliteGraphStore(tmp_path / "c.db")
    try:
        _seed(s.merge_node)
        s.merge_node("Tool", "name", "graph-secret", OTHER.to_properties())
        names = [c["name"] for c in s.name_candidates("graph-store", scope=OWNER)]
        assert names[0] == "graph-store"  # exact first
        assert "graph-sync" in names
        assert "graph-secret" not in names  # other owner
        assert "graph review" not in names  # system Insights never resolve
        assert [c["name"] for c in s.name_candidates("100% cov", scope=OWNER)] == ["100% coverage"]
        assert s.name_candidates("graph", scope=None) == []  # fail-closed
    finally:
        s.close()


@pytest.mark.neo4j
def test_neo4j_sync_name_candidates() -> None:
    from engrama.backends.neo4j.backend import Neo4jGraphStore
    from engrama.core.client import EngramaClient

    p = f"rs{uuid.uuid4().hex[:6]}-"
    client = EngramaClient()
    s = Neo4jGraphStore(client)
    try:
        _seed(s.merge_node, p)
        rows = s.name_candidates(f"{p}graph-store", scope=OWNER)
        assert rows[0]["name"] == f"{p}graph-store"
        assert f"{p}graph review" not in {r["name"] for r in rows}
    finally:
        client.run(
            "MATCH (n) WHERE coalesce(n.name, n.title) STARTS WITH $p DETACH DELETE n", {"p": p}
        )
        client.close()


# --- MCP remember ---------------------------------------------------------------


class _Embedder:
    """Deterministic embedder: same text family → same direction."""

    dimensions = 768

    def __init__(self) -> None:
        self.calls = 0

    async def aembed(self, text: str) -> list[float]:
        self.calls += 1
        base = [1.0] * 768 if "persist" in text.lower() else [0.0] * 767 + [1.0]
        return base


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in (
        "ENGRAMA_ORG_ID",
        "ENGRAMA_USER_ID",
        "ENGRAMA_LOCAL_SUB",
        "VAULT_PATH",
        "ENGRAMA_REMEMBER_DEDUPE",
        "ENGRAMA_TAG_LINKING",
    ):
        monkeypatch.delenv(var, raising=False)


async def _call(server, monkeypatch, tool: str, params: dict) -> dict:
    from mcp.client import Client

    from engrama.adapters.mcp import server as srv_module

    monkeypatch.setattr(srv_module, "resolve_scope", lambda _ctx: OWNER)
    async with Client(server) as client:
        result = await client.call_tool(tool, {"params": params})
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


def _sqlite_server(db: Path):
    from engrama.adapters.mcp.server import create_engrama_mcp

    return create_engrama_mcp(backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)})


async def test_inline_target_label_conflict_and_revive(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "a.db"
    s = SqliteGraphStore(db)
    s.merge_node("Client", "name", "Atlas", OWNER.to_properties())
    s.merge_node("Project", "name", "Legacy", {"status": "archived", **OWNER.to_properties()})
    s.close()
    out = await _call(
        _sqlite_server(db),
        monkeypatch,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {"name": "note"},
            "relations": {
                "BELONGS_TO": [{"name": "Atlas", "label": "Project"}, "Legacy"],
            },
        },
    )
    reasons = {a["target"]: a["reason"] for a in out["relations_ambiguous"]}
    assert reasons == {"Atlas": "label_conflict", "Legacy": "revive_candidate"}
    assert out["relations_created"] == 0
    assert "relations_stubbed" not in out


async def test_new_node_reports_possible_duplicates_only_on_creation(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "b.db"
    s = SqliteGraphStore(db)
    s.merge_node("Tool", "name", "graph-store", OWNER.to_properties())
    s.close()
    server = _sqlite_server(db)
    created = await _call(
        server,
        monkeypatch,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "Graph Store"}},
    )
    assert created["possible_duplicate_of"][0]["reason"] == "same name, different label"
    updated = await _call(
        server,
        monkeypatch,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "Graph Store", "summary": "x"}},
    )
    assert "possible_duplicate_of" not in updated


async def test_block_mode_refuses_until_forced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ENGRAMA_REMEMBER_DEDUPE", "block")
    db = tmp_path / "c.db"
    s = SqliteGraphStore(db)
    s.merge_node("Tool", "name", "graph-store", OWNER.to_properties())
    s.close()
    server = _sqlite_server(db)
    refused = await _call(
        server,
        monkeypatch,
        "engrama_remember",
        {"label": "Tool", "properties": {"name": "graph-stores"}},
    )
    assert refused["status"] == "error"
    assert refused["possible_duplicate_of"][0]["name"] == "graph-store"
    s = SqliteGraphStore(db)
    assert s.get_node("Tool", "name", "graph-stores", scope=OWNER) is None
    s.close()
    forced = await _call(
        server,
        monkeypatch,
        "engrama_remember",
        {"label": "Tool", "properties": {"name": "graph-stores"}, "force_new": True},
    )
    assert forced["status"] == "ok"


async def test_semantic_duplicate_and_single_embed_per_write(tmp_path: Path, monkeypatch) -> None:
    from engrama import backends

    embedder = _Embedder()
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    monkeypatch.setattr(backends, "create_embedding_provider", lambda *a, **k: embedder)
    server = _sqlite_server(tmp_path / "d.db")
    first = await _call(
        server,
        monkeypatch,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {"name": "storage tier", "summary": "persistence layer"},
        },
    )
    assert first["embedded"] is True
    assert embedder.calls == 1  # one embed per write: the vector really landed
    second = await _call(
        server,
        monkeypatch,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {"name": "durable disk", "summary": "persistent storage"},
        },
    )
    assert [(d["name"], d["reason"]) for d in second["possible_duplicate_of"]] == [
        ("storage tier", "similar meaning")
    ]


@pytest.mark.neo4j
async def test_neo4j_semantic_duplicate(monkeypatch) -> None:
    from engrama import backends
    from engrama.adapters.mcp.server import create_engrama_mcp

    embedder = _Embedder()
    monkeypatch.setattr(backends, "create_embedding_provider", lambda *a, **k: embedder)
    owner = MemoryScope(org_id=f"res-{uuid.uuid4().hex[:6]}", user_id="u")
    cfg = {
        k: os.environ[k]
        for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")
        if k in os.environ
    }
    cfg["EMBEDDING_DIMENSIONS"] = "768"
    server = create_engrama_mcp(backend="neo4j", config=cfg)
    from mcp.client import Client

    from engrama.adapters.mcp import server as srv_module

    monkeypatch.setattr(srv_module, "resolve_scope", lambda _ctx: owner)
    try:
        async with Client(server) as client:
            await client.call_tool(
                "engrama_remember",
                {
                    "params": {
                        "label": "Concept",
                        "properties": {"name": "storage tier", "summary": "persistence layer"},
                    }
                },
            )
            r = await client.call_tool(
                "engrama_remember",
                {
                    "params": {
                        "label": "Concept",
                        "properties": {"name": "durable disk", "summary": "persistent storage"},
                    }
                },
            )
        out = json.loads(r.content[0].text)  # type: ignore[union-attr]
        assert ("storage tier", "similar meaning") in {
            (d["name"], d["reason"]) for d in out.get("possible_duplicate_of", [])
        }
    finally:
        from engrama.core.client import EngramaClient

        c = EngramaClient()
        c.run("MATCH (n) WHERE n.org_id = $o DETACH DELETE n", {"o": owner.org_id})
        c.close()
