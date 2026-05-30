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
