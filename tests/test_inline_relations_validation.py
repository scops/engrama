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
