"""Inline relations surface rejected (unknown) relation types in the response.

Asymmetry fixed here: ``engrama_relate`` returns a hard error for an unknown
``rel_type``, but the inline ``relations={...}`` path used to skip unknown types
with only a server-log warning — the caller saw ``status: "ok"`` /
``relations_created: 0`` with no idea a relation was dropped. The response now
carries ``relations_rejected`` so the rejection is visible to the client.

SQLite backend, in-process, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engrama.adapters.mcp.server import create_engrama_mcp

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in ("ENGRAMA_ORG_ID", "ENGRAMA_USER_ID", "ENGRAMA_AGENT_ID", "VAULT_PATH"):
        monkeypatch.delenv(var, raising=False)


def _server(db: Path):
    return create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)}, vault_path=None
    )


async def _call(server, tool: str, args: dict) -> dict:
    from fastmcp import Client

    async with Client(server) as client:
        result = await client.call_tool(tool, {"params": args})
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def test_unknown_inline_rel_type_is_surfaced(tmp_path: Path) -> None:
    server = _server(tmp_path / "engrama.db")
    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {"name": "src", "relations": {"AFFECTS": ["something"]}},
        },
    )
    assert resp.get("status") == "ok"
    assert resp.get("relations_created") == 0
    assert resp.get("relations_rejected") == ["AFFECTS"]
    assert "relations_rejected_note" in resp


async def test_mixed_valid_and_invalid_rel_types(tmp_path: Path) -> None:
    server = _server(tmp_path / "engrama.db")
    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {
                "name": "src2",
                "relations": {"RELATED_TO": ["valid-target"], "AFFECTS": ["x"]},
            },
        },
    )
    assert resp.get("status") == "ok"
    # The valid relation lands; the unknown one is reported, not silently dropped.
    assert resp.get("relations_created") == 1
    assert resp.get("relations_rejected") == ["AFFECTS"]


async def test_all_valid_rel_types_have_no_rejected_key(tmp_path: Path) -> None:
    server = _server(tmp_path / "engrama.db")
    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {"name": "src3", "relations": {"RELATED_TO": ["t"]}},
        },
    )
    assert resp.get("status") == "ok"
    assert resp.get("relations_created") == 1
    assert "relations_rejected" not in resp


# --- #93: silent relation failures are now surfaced ---


async def test_inline_relation_to_missing_target_reports_stub(tmp_path: Path) -> None:
    """Mode 1: the target name resolves to nothing, so a stub is created and
    the edge lands — but the caller is told it linked to a NEW node, not a
    pre-existing one, so an orphan stub isn't mistaken for the intended link.
    """
    server = _server(tmp_path / "engrama.db")
    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Problem",
            "properties": {
                "title": "noisy-reflect",
                "relations": {"RELATED_TO": ["Engrama"]},
            },
        },
    )
    assert resp.get("status") == "ok"
    # The edge to the stub still counts as created.
    assert resp.get("relations_created") == 1
    assert "relations_failed" not in resp
    stubbed = resp.get("relations_stubbed")
    assert stubbed is not None
    assert {s["target"] for s in stubbed} == {"Engrama"}
    assert "relations_stubbed_note" in resp


async def test_inline_relation_match_failure_is_surfaced(tmp_path: Path) -> None:
    """Mode 2: the target resolves by name (case-insensitive lookup) but the
    edge MERGE matches on the exact key and finds nothing, so no edge is
    created. This used to return relations_created:0 with status:ok and only a
    server-side log; now the dropped relation is reported.
    """
    server = _server(tmp_path / "engrama.db")
    # An existing, correctly-keyed target.
    pre = await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "ExistingTarget"}},
    )
    assert pre.get("status") == "ok"

    # Relate to it by a name that only differs in case: lookup_node_label
    # finds it (LOWER match) so no stub is created, but merge_relation's exact
    # key match misses, dropping the edge.
    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Problem",
            "properties": {
                "title": "src-problem",
                "relations": {"RELATED_TO": ["existingtarget"]},
            },
        },
    )
    assert resp.get("status") == "ok"
    assert resp.get("relations_created") == 0
    assert "relations_stubbed" not in resp
    failed = resp.get("relations_failed")
    assert failed is not None
    assert {f["target"] for f in failed} == {"existingtarget"}
    assert all(f["reason"] == "match_failed" for f in failed)
    assert "relations_failed_note" in resp


async def test_relate_reports_which_endpoint_is_missing(tmp_path: Path) -> None:
    """engrama_relate names the missing endpoint instead of a vague 'could not
    find either' (#93)."""
    server = _server(tmp_path / "engrama.db")
    await _call(server, "engrama_remember", {"label": "Concept", "properties": {"name": "a"}})
    resp = await _call(
        server,
        "engrama_relate",
        {
            "from_name": "a",
            "from_label": "Concept",
            "rel_type": "RELATED_TO",
            "to_name": "ghost",
            "to_label": "Concept",
        },
    )
    assert resp.get("status") == "error"
    err = resp.get("error", "")
    assert "ghost" in err and "not found" in err
    # The present 'from' endpoint must not be flagged as missing.
    assert "from (" not in err


async def test_relate_reports_label_mismatch(tmp_path: Path) -> None:
    """When the endpoint exists under a different label, say so (#93)."""
    server = _server(tmp_path / "engrama.db")
    await _call(server, "engrama_remember", {"label": "Concept", "properties": {"name": "a"}})
    await _call(server, "engrama_remember", {"label": "Concept", "properties": {"name": "thing"}})
    resp = await _call(
        server,
        "engrama_relate",
        {
            "from_name": "a",
            "from_label": "Concept",
            "rel_type": "RELATED_TO",
            "to_name": "thing",
            "to_label": "Problem",
        },
    )
    assert resp.get("status") == "error"
    err = resp.get("error", "")
    assert "thing" in err and ":Concept" in err and "fix the label" in err


# --- #93 follow-up: confidence-gated three-way resolution (connect / ask / create) ---


async def test_inline_relation_fuzzy_connects_to_near_certain_match(tmp_path: Path) -> None:
    """Path 1: a near-identical in-scope node wins, so the edge connects to it
    instead of minting an orphan stub — and the auto-connection is reported,
    never silent."""
    server = _server(tmp_path / "engrama.db")
    await _call(
        server,
        "engrama_remember",
        {"label": "Project", "properties": {"name": "engrama-saas"}},
    )
    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Problem",
            "properties": {
                "title": "noisy-prob",
                "relations": {"RELATED_TO": ["engrama-sas"]},  # typo of engrama-saas
            },
        },
    )
    assert resp.get("status") == "ok"
    assert resp.get("relations_created") == 1
    assert "relations_stubbed" not in resp
    resolved = resp.get("relations_resolved")
    assert resolved is not None
    assert resolved[0]["target"] == "engrama-sas"
    assert resolved[0]["resolved_to"] == "engrama-saas"
    assert resolved[0]["resolved_by"] == "fuzzy_match"


async def test_inline_relation_ambiguous_asks_instead_of_connecting(tmp_path: Path) -> None:
    """Path 2: candidates exist but none clearly wins, so NOTHING is created and
    the in-scope candidates come back as did_you_mean. When in doubt, ask."""
    server = _server(tmp_path / "engrama.db")
    for name in ("engrama-saas", "engrama-core"):
        await _call(server, "engrama_remember", {"label": "Project", "properties": {"name": name}})
    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Problem",
            "properties": {
                "title": "amb-prob",
                "relations": {"RELATED_TO": ["engrama"]},
            },
        },
    )
    assert resp.get("status") == "ok"
    # Grey zone: no edge, no stub.
    assert resp.get("relations_created") == 0
    assert "relations_stubbed" not in resp
    assert "relations_resolved" not in resp
    amb = resp.get("relations_ambiguous")
    assert amb is not None
    suggested = {c["name"] for c in amb[0]["did_you_mean"]}
    assert {"engrama-saas", "engrama-core"} <= suggested


async def test_inline_relation_other_tenant_target_never_leaks(tmp_path: Path) -> None:
    """Hard isolation constraint: a node that exists ONLY in another tenant must
    never be fuzzy-connected to and never surface in did_you_mean, even on a
    near-identical name. Candidate discovery is scope-filtered, so the standalone
    caller can't see — let alone link to — tenant B's node."""
    from engrama import Engrama

    db = tmp_path / "shared.db"
    # Tenant B owns a node whose name is a near-match for what the caller types.
    with Engrama(backend="sqlite", db_path=db, org_id="globex", user_id="bob") as eng:
        eng.remember("Project", "engrama-saas", "tenant B private project")

    # The MCP server runs standalone (a different scope from tenant B).
    server = _server(db)
    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Problem",
            "properties": {
                "title": "probe",
                "relations": {"RELATED_TO": ["engrama-sas"]},  # ~0.96 vs B's node
            },
        },
    )
    assert resp.get("status") == "ok"
    # Never connected to B's node, never suggested it.
    assert "relations_resolved" not in resp
    # B's exact name must not appear anywhere in the response.
    assert "engrama-saas" not in json.dumps(resp)

    # And B's node must be untouched — no cross-tenant edge formed.
    from engrama.backends.sqlite.store import SqliteGraphStore
    from engrama.core.scope import MemoryScope

    store = SqliteGraphStore(db)
    try:
        nbrs = store.get_neighbours(
            "Project", "name", "engrama-saas", scope=MemoryScope(org_id="globex", user_id="bob")
        )
    finally:
        store.close()
    assert nbrs == []
