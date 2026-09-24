"""Reflect detectors (DDR-008): live nodes only, aggregated, same on every backend."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from engrama.core.reflection import (
    BUDGET_PER_DETECTOR,
    DETECTORS,
    UNDER_CONNECTED_TITLE,
    InsightDraft,
    select,
)
from engrama.core.scope import MemoryScope

# --- pure pieces --------------------------------------------------------------


def _detector(name: str):
    return next(d for d in DETECTORS if d.name == name)


def test_activation_rules() -> None:
    assert _detector("shared_technology").applies({"Technology": 1}) is True
    assert _detector("shared_technology").applies({"Project": 3}) is False
    training = _detector("training_opportunity")
    assert training.applies({"Vulnerability": 1, "Course": 1}) is True
    assert training.applies({"Course": 1}) is False
    assert _detector("technique_transfer").applies({"Technique": 1, "Domain": 1}) is False
    assert _detector("under_connected").applies({"Concept": 4}) is False
    assert _detector("under_connected").applies({"Concept": 5}) is True


def test_select_skips_judged_and_repeats_and_applies_the_budget() -> None:
    drafts = [InsightDraft(f"t{i % 40}", "b", 0.5, "q") for i in range(80)]
    kept = select(drafts, judged={"t0", "t1"})
    assert len(kept) == BUDGET_PER_DETECTOR
    assert {d.title for d in kept}.isdisjoint({"t0", "t1"})
    assert len({d.title for d in kept}) == len(kept)


def test_titles_are_stable_across_counts() -> None:
    build = _detector("shared_technology").build
    three = build(
        [{"technology": "py", "members": [{"name": n, "label": "Project"} for n in "abc"]}]
    )
    four = build(
        [{"technology": "py", "members": [{"name": n, "label": "Project"} for n in "abcd"]}]
    )
    assert three[0].title == four[0].title == "Shared technology: py"
    assert ("Technology", "py") in three[0].about


# --- one graph, every backend -----------------------------------------------

OWNER = MemoryScope(org_id="reflect-org", user_id="reflect-user")


def _seed(merge_node, merge_relation, p: str) -> None:
    """``p`` prefixes every name so a shared Neo4j can't mix runs."""

    def node(label, name, **props):
        key = "title" if label in {"Decision", "Problem", "Insight"} else "name"
        merge_node(label, key, f"{p}{name}", {**props, **OWNER.to_properties()})

    def rel(fl, fn, rt, tl, tn):
        fk = "title" if fl in {"Decision", "Problem", "Insight"} else "name"
        tk = "title" if tl in {"Decision", "Problem", "Insight"} else "name"
        merge_relation(fl, fk, f"{p}{fn}", rt, tl, tk, f"{p}{tn}", scope=OWNER)

    node("Technology", "python")
    node("Technology", "rust")
    for proj in ("alpha", "beta"):
        node("Project", proj, status="active")
        rel("Project", proj, "USES", "Technology", "python")
        rel("Project", proj, "USES", "Technology", "rust")
    # A hand-written Insight is a domain node: it counts as a third user.
    node("Insight", "lesson", body="hand-written")
    rel("Insight", "lesson", "USES", "Technology", "python")
    # Archived and superseded users never count.
    node("Project", "old", status="archived")
    rel("Project", "old", "USES", "Technology", "rust")
    node("Project", "gone", status="superseded")
    rel("Project", "gone", "USES", "Technology", "rust")
    # Concept cluster: three live members plus a superseded one.
    node("Concept", "graphs")
    for member in ("m1", "m2", "m3"):
        node("Concept", member)
        rel("Concept", member, "INSTANCE_OF", "Concept", "graphs")
    node("Concept", "m4", status="superseded")
    rel("Concept", "m4", "INSTANCE_OF", "Concept", "graphs")
    # Under-connected: no status at all (the null-safe case), a stub-only
    # neighbour, and an archived orphan that must not be reported.
    node("Concept", "lonely")
    node("Concept", "stubbed")
    node("Technology", "placeholder", status="stub")
    rel("Concept", "stubbed", "USES", "Technology", "placeholder")
    node("Concept", "buried", status="archived")
    # Stale: low confidence, linked to an active project.
    node("Concept", "shaky", confidence=0.1)
    rel("Concept", "shaky", "BELONGS_TO", "Project", "alpha")


def _expected(p: str) -> dict:
    return {
        "shared_technology": {(f"{p}python", frozenset({f"{p}alpha", f"{p}beta", f"{p}lesson"}))},
        "concept_clusters": {(f"{p}graphs", 3)},
        "stale": {(f"{p}shaky", f"{p}alpha")},
        "under_connected_includes": {f"{p}lonely", f"{p}stubbed", f"{p}placeholder"},
        "under_connected_excludes": {f"{p}buried", f"{p}old", f"{p}gone", f"{p}m4"},
    }


def _observed(rows: dict, p: str) -> dict:
    mine = lambda name: name.startswith(p)  # noqa: E731
    return {
        "shared_technology": {
            (r["technology"], frozenset(m["name"] for m in r["members"]))
            for r in rows["shared"]
            if mine(r["technology"])
        },
        "concept_clusters": {
            (r["concept"], r["entity_count"]) for r in rows["clusters"] if mine(r["concept"])
        },
        "stale": {(r["name"], r["project"]) for r in rows["stale"] if mine(r["name"])},
        "under_connected": {r["name"] for r in rows["under"] if mine(r["name"])},
    }


def _check(observed: dict, p: str) -> None:
    exp = _expected(p)
    assert observed["shared_technology"] == exp["shared_technology"]
    assert observed["concept_clusters"] == exp["concept_clusters"]
    assert observed["stale"] == exp["stale"]
    assert exp["under_connected_includes"] <= observed["under_connected"]
    assert not exp["under_connected_excludes"] & observed["under_connected"]


def _sync_rows(store) -> dict:
    return {
        "shared": store.detect_shared_technology(scope=OWNER),
        "clusters": store.detect_concept_clusters(scope=OWNER),
        "stale": store.detect_stale_knowledge(scope=OWNER),
        "under": store.detect_under_connected_nodes(scope=OWNER),
    }


def test_sqlite_detectors(tmp_path: Path) -> None:
    from engrama.backends.sqlite.store import SqliteGraphStore

    store = SqliteGraphStore(tmp_path / "reflect.db")
    try:
        _seed(store.merge_node, store.merge_relation, "")
        _check(_observed(_sync_rows(store), ""), "")
    finally:
        store.close()


@pytest.mark.neo4j
def test_neo4j_sync_detectors() -> None:
    from engrama.backends.neo4j.backend import Neo4jGraphStore
    from engrama.core.client import EngramaClient

    p = f"rd{uuid.uuid4().hex[:6]}-"
    client = EngramaClient()
    store = Neo4jGraphStore(client)
    try:
        _seed(store.merge_node, store.merge_relation, p)
        _check(_observed(_sync_rows(store), p), p)
    finally:
        client.run(
            "MATCH (n) WHERE coalesce(n.name, n.title) STARTS WITH $p DETACH DELETE n", {"p": p}
        )
        client.close()


@pytest.mark.neo4j
async def test_neo4j_async_detectors() -> None:
    from engrama.backends import create_async_stores

    p = f"ra{uuid.uuid4().hex[:6]}-"
    store, _ = create_async_stores({"GRAPH_BACKEND": "neo4j"})
    pending: list = []
    try:
        _seed(
            lambda *a: pending.append(("node", a)),
            lambda *a, **k: pending.append(("rel", a, k)),
            p,
        )
        for item in pending:
            if item[0] == "node":
                await store.merge_node(*item[1])
            else:
                await store.merge_relation(*item[1], **item[2])
        rows = {
            "shared": await store.detect_shared_technology(scope=OWNER),
            "clusters": await store.detect_concept_clusters(scope=OWNER),
            "stale": await store.detect_stale_knowledge(scope=OWNER),
            "under": await store.detect_under_connected_nodes(scope=OWNER),
        }
        _check(_observed(rows, p), p)
    finally:
        await store._driver.execute_query(
            "MATCH (n) WHERE coalesce(n.name, n.title) STARTS WITH $p DETACH DELETE n",
            parameters_={"p": p},
        )
        await store.close()


# --- entry points -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in ("ENGRAMA_ORG_ID", "ENGRAMA_USER_ID", "ENGRAMA_LOCAL_SUB", "VAULT_PATH"):
        monkeypatch.delenv(var, raising=False)


def test_sdk_reflect_never_undoes_an_approval(tmp_path: Path) -> None:
    from engrama import Engrama

    db = tmp_path / "sdk.db"
    with Engrama(backend="sqlite", db_path=db, org_id=OWNER.org_id, user_id=OWNER.user_id) as eng:
        _seed(eng._store.merge_node, eng._store.merge_relation, "")
        first = {i.title for i in eng.reflect()}
        assert "Shared technology: python" in first
        assert eng.approve_insight("Shared technology: python")["matched"] is True
        eng.reflect()
        status = eng._store.get_insight_by_title("Shared technology: python", scope=OWNER)
        assert status["status"] == "approved"


async def test_mcp_reflect_respects_a_dismissed_under_connected_insight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.client import Client

    from engrama.adapters.mcp import server as srv_module
    from engrama.adapters.mcp.server import create_engrama_mcp
    from engrama.backends.sqlite.store import SqliteGraphStore

    db = tmp_path / "mcp.db"
    seed = SqliteGraphStore(db)
    _seed(seed.merge_node, seed.merge_relation, "")
    # A dismissal recorded under an older title must still hold.
    seed.merge_node(
        "Insight",
        "title",
        "Under-connected nodes (legacy title)",
        {"status": "dismissed", "source_query": "under_connected", **OWNER.to_properties()},
    )
    seed.close()

    server = create_engrama_mcp(backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)})
    monkeypatch.setattr(srv_module, "resolve_scope", lambda _ctx: OWNER)
    async with Client(server) as client:
        result = await client.call_tool("engrama_reflect", {})
        payload = json.loads(result.content[0].text)  # type: ignore[union-attr]

    queries = {i["query"] for i in payload["insights"]}
    assert "shared_technology" in queries
    assert "under_connected" not in queries
    assert UNDER_CONNECTED_TITLE not in {i["title"] for i in payload["insights"]}
