"""Owner-scoped node identity.

A node's identity is ``(label, name|title, org_id, user_id)``: two owners can
hold nodes with the same name, and every path that addresses a node by key —
merge, recall, Insight status/sync, embeddings, forget — works on the caller's
own node. Also covers the in-place migration of pre-v3 SQLite databases.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from engrama import Engrama
from engrama.backends.sqlite.store import SqliteGraphStore
from engrama.backends.sqlite.vector import SqliteVecStore
from engrama.core.scope import MemoryScope

ALICE = MemoryScope(org_id="acme", user_id="alice")
BOB = MemoryScope(org_id="globex", user_id="bob")


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


def _sdk(db: Path, scope: MemoryScope) -> Engrama:
    return Engrama(backend="sqlite", db_path=db, org_id=scope.org_id, user_id=scope.user_id)


def _owners(db: Path, label: str, key: str) -> set[tuple[str, str]]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT props FROM nodes WHERE label = ? AND key_value = ?", (label, key)
        ).fetchall()
    finally:
        conn.close()
    return {(json.loads(p)["org_id"], json.loads(p)["user_id"]) for (p,) in rows}


def test_same_name_gives_each_owner_its_own_node(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    with _sdk(db, ALICE) as eng:
        eng.remember("Project", "roadmap", "alice notes", details="ACME acquisition plan")
    with _sdk(db, BOB) as eng:
        eng.remember("Project", "roadmap", "globex public roadmap")
        bob_view = eng.recall("roadmap")

    # One node per owner — Bob's write created his own instead of merging.
    assert _owners(db, "Project", "roadmap") == {("acme", "alice"), ("globex", "bob")}
    # Bob never sees Alice's properties...
    assert [r.properties.get("details") for r in bob_view] == [None]
    # ...and Alice still owns hers, untouched.
    with _sdk(db, ALICE) as eng:
        alice_view = eng.recall("roadmap")
    assert [(r.properties["org_id"], r.properties.get("details")) for r in alice_view] == [
        ("acme", "ACME acquisition plan")
    ]


def test_rewrite_by_same_owner_still_merges(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    with _sdk(db, ALICE) as eng:
        eng.remember("Project", "roadmap", "v1")
        eng.remember("Project", "roadmap", "v2")
    assert _owners(db, "Project", "roadmap") == {("acme", "alice")}


@pytest.mark.parametrize("purge", [False, True])
def test_forget_only_reaches_own_node(tmp_path: Path, purge: bool) -> None:
    db = tmp_path / "shared.db"
    for scope in (ALICE, BOB):
        with _sdk(db, scope) as eng:
            eng.remember("Project", "roadmap", f"{scope.user_id} notes")
    with _sdk(db, BOB) as eng:
        assert eng.forget("Project", "roadmap", purge=purge)["matched"] is True
    with _sdk(db, ALICE) as eng:
        alice = eng.recall("roadmap")
    assert [(r.properties["user_id"], r.properties.get("status")) for r in alice] == [
        ("alice", None)
    ]
    # Forgetting a name you don't own matches nothing.
    with _sdk(db, MemoryScope(org_id="initech", user_id="eve")) as eng:
        assert eng.forget("Project", "roadmap", purge=True)["matched"] is False


def _seed_insight(store: SqliteGraphStore, scope: MemoryScope, title: str) -> None:
    store.merge_node(
        "Insight",
        "title",
        title,
        {"status": "pending", "body": f"{scope.user_id} insight", **scope.to_properties()},
    )


def test_insight_status_and_sync_stay_in_owner_scope(tmp_path: Path) -> None:
    # Insight titles are templated, so two tenants routinely share one.
    title = "Concept cluster: python (3 entities)"
    store = SqliteGraphStore(tmp_path / "insights.db")
    try:
        _seed_insight(store, BOB, title)
        _seed_insight(store, ALICE, title)

        assert store.update_insight_status(title, "approved", scope=ALICE) is True
        assert store.mark_insight_synced(title, "notes/alice.md", scope=ALICE) is True

        assert store.get_insight_by_title(title, scope=ALICE)["status"] == "approved"
        assert store.get_insight_by_title(title, scope=BOB)["status"] == "pending"
        props = {
            json.loads(p)["user_id"]: json.loads(p)
            for (p,) in store._conn.execute(
                "SELECT props FROM nodes WHERE label = 'Insight' AND key_value = ?", (title,)
            )
        }
        assert props["alice"]["obsidian_path"] == "notes/alice.md"
        assert "obsidian_path" not in props["bob"]
        # A scope that can't see the Insight updates nothing.
        stranger = MemoryScope(org_id="initech", user_id="eve")
        assert store.update_insight_status(title, "dismissed", scope=stranger) is False
    finally:
        store.close()


def test_sdk_approve_only_touches_own_insight(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    title = "Shared technology: rust (a & b)"
    store = SqliteGraphStore(db)
    _seed_insight(store, BOB, title)
    _seed_insight(store, ALICE, title)
    store.close()

    with _sdk(db, ALICE) as eng:
        assert eng.approve_insight(title)["matched"] is True
    with _sdk(db, BOB) as eng:
        assert eng._store.get_insight_by_title(title, scope=BOB)["status"] == "pending"


def test_embedding_lands_on_owner_node(tmp_path: Path) -> None:
    graph = SqliteGraphStore(tmp_path / "vec.db")
    vec = SqliteVecStore(graph._conn, dimensions=4)
    vec.ensure_index()
    try:
        for scope in (ALICE, BOB):
            graph.merge_node("Concept", "name", "shared", scope.to_properties())
        assert vec.store_vector_by_key("Concept", "name", "shared", [1, 0, 0, 0], owner=BOB)
        rows = graph._conn.execute(
            f"SELECT n.props FROM nodes n JOIN {vec._index_name} v ON v.node_id = n.id"
        ).fetchall()
        assert [json.loads(p)["user_id"] for (p,) in rows] == ["bob"]
        # No owner → only an identity-less node of that name, which doesn't exist.
        assert vec.store_vector_by_key("Concept", "name", "shared", [0, 1, 0, 0]) is False
    finally:
        graph.close()


def test_legacy_database_is_migrated_in_place(tmp_path: Path) -> None:
    """A pre-v3 file (table-level UNIQUE(label, key_value)) is rebuilt on
    connect: ids, edges and FTS rows survive, and same names become legal."""
    db = tmp_path / "legacy.db"
    store = SqliteGraphStore(db)
    store.merge_node("Project", "name", "roadmap", ALICE.to_properties())
    store.merge_node("Concept", "name", "plans", ALICE.to_properties())
    store.merge_relation(
        "Project", "name", "roadmap", "RELATED_TO", "Concept", "name", "plans", scope=ALICE
    )
    ids_before = dict(store._conn.execute("SELECT key_value, id FROM nodes").fetchall())
    store.close()

    # Recreate the old table shape: identical columns, legacy UNIQUE constraint.
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        DROP INDEX idx_nodes_identity;
        CREATE TABLE nodes_old (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
            key_field TEXT NOT NULL, key_value TEXT NOT NULL,
            props TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, UNIQUE(label, key_value)
        );
        INSERT INTO nodes_old SELECT * FROM nodes;
        DROP TABLE nodes;
        ALTER TABLE nodes_old RENAME TO nodes;
        """
    )
    conn.close()

    store = SqliteGraphStore(db)
    try:
        origins = {r["origin"] for r in store._conn.execute("PRAGMA index_list(nodes)")}
        assert "u" not in origins  # legacy constraint gone
        assert dict(store._conn.execute("SELECT key_value, id FROM nodes").fetchall()) == (
            ids_before
        )
        assert store.get_neighbours("Project", "name", "roadmap", scope=ALICE)
        assert store.fulltext_search("roadmap", scope=ALICE)

        store.merge_node("Project", "name", "roadmap", BOB.to_properties())
        assert _owners(db, "Project", "roadmap") == {("acme", "alice"), ("globex", "bob")}
        # Idempotent: a second connect doesn't rebuild again.
        assert store._migrate_node_identity() is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_mcp_remember_same_name_does_not_cross_tenants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.client import Client

    from engrama.adapters.mcp import server as srv_module
    from engrama.adapters.mcp.server import create_engrama_mcp

    db = tmp_path / "mcp.db"
    server = create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)}, vault_path=None
    )
    caller = {"scope": ALICE}
    monkeypatch.setattr(srv_module, "resolve_scope", lambda _ctx: caller["scope"])

    async def call(client: Client, tool: str, args: dict) -> dict:
        result = await client.call_tool(tool, {"params": args})
        return json.loads(result.content[0].text)  # type: ignore[union-attr]

    async with Client(server) as client:
        await call(
            client,
            "engrama_remember",
            {"label": "Project", "properties": {"name": "roadmap", "details": "acme secret"}},
        )
        caller["scope"] = BOB
        await call(
            client,
            "engrama_remember",
            {"label": "Project", "properties": {"name": "roadmap", "summary": "globex"}},
        )
        bob_ctx = await call(client, "engrama_context", {"name": "roadmap", "label": "Project"})

    assert _owners(db, "Project", "roadmap") == {("acme", "alice"), ("globex", "bob")}
    assert "acme secret" not in json.dumps(bob_ctx)


@pytest.mark.neo4j
@pytest.mark.asyncio
async def test_neo4j_same_name_is_owner_scoped() -> None:
    """Same contract on Neo4j, where the owner is part of the MERGE pattern
    and the key constraint is (key, org_id, user_id)."""
    from engrama.backends import create_async_stores

    store, _ = create_async_stores({"GRAPH_BACKEND": "neo4j"})
    await store.ensure_schema()
    name = f"same-name-{uuid.uuid4().hex[:8]}"
    try:
        await store.merge_node(
            "Project", "name", name, {"details": "acme secret", **ALICE.to_properties()}
        )
        await store.merge_node(
            "Project", "name", name, {"summary": "globex", **BOB.to_properties()}
        )
        await store.merge_node(
            "Project", "name", name, {"summary": "globex v2", **BOB.to_properties()}
        )
        bob = await store.get_node("Project", "name", name, scope=BOB)
        alice = await store.get_node("Project", "name", name, scope=ALICE)
        assert (bob["org_id"], bob["summary"], bob.get("details")) == ("globex", "globex v2", None)
        assert (alice["org_id"], alice["details"]) == ("acme", "acme secret")

        for scope in (BOB, ALICE):
            await store.merge_node(
                "Insight", "title", name, {"status": "pending", **scope.to_properties()}
            )
        assert await store.update_insight_status(name, "approved", scope=ALICE) is True
        assert (await store.get_insight_by_title(name, scope=ALICE))["status"] == "approved"
        assert (await store.get_insight_by_title(name, scope=BOB))["status"] == "pending"
    finally:
        await store._driver.execute_query(
            "MATCH (n) WHERE n.name = $k OR n.title = $k DETACH DELETE n",
            parameters_={"k": name},
        )
        await store.close()


def test_export_import_round_trip_keeps_edges_and_vectors_on_their_owner(tmp_path: Path) -> None:
    from engrama.migrate import export_graph, import_graph

    def stores(db: Path) -> tuple[SqliteGraphStore, SqliteVecStore]:
        g = SqliteGraphStore(db)
        v = SqliteVecStore(g._conn, dimensions=4)
        v.ensure_index()
        return g, v

    src_g, src_v = stores(tmp_path / "src.db")
    for scope, other in ((ALICE, "alice-only"), (BOB, "bob-only")):
        src_g.merge_node("Project", "name", "roadmap", scope.to_properties())
        src_g.merge_node("Concept", "name", other, scope.to_properties())
        src_g.merge_relation(
            "Project", "name", "roadmap", "RELATED_TO", "Concept", "name", other, scope=scope
        )
    src_v.store_vector_by_key("Project", "name", "roadmap", [0, 0, 0, 1], owner=BOB)
    dump = tmp_path / "dump.ndjson"
    export_graph(src_g, src_v, dump)
    src_g.close()

    dst_g, dst_v = stores(tmp_path / "dst.db")
    try:
        import_graph(dst_g, dst_v, dump)
        for scope, other in ((ALICE, "alice-only"), (BOB, "bob-only")):
            names = {
                r["neighbour"]["name"]
                for r in dst_g.get_neighbours("Project", "name", "roadmap", scope=scope)
            }
            assert names == {other}
        owners = [
            json.loads(p)["user_id"]
            for (p,) in dst_g._conn.execute(
                f"SELECT n.props FROM nodes n JOIN {dst_v._index_name} v ON v.node_id = n.id"
            )
        ]
        assert owners == ["bob"]
    finally:
        dst_g.close()
