"""Standalone single-user MCP flow (Spec 001, T016 / US-1, T-5).

With no inbound identity headers (bare OSS, no gateway), the MCP server
resolves a single stable ``sub_local`` and writes/reads under it. A remember
followed by a search must round-trip transparently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engrama.adapters.mcp.server import create_engrama_mcp

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in (
        "ENGRAMA_ORG_ID",
        "ENGRAMA_USER_ID",
        "ENGRAMA_AGENT_ID",
        "ENGRAMA_SESSION_ID",
        "VAULT_PATH",
        "ENGRAMA_LOCAL_SUB",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_server(db: Path):
    return create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)}, vault_path=None
    )


async def _call(server, tool: str, args: dict | None = None):
    from fastmcp import Client

    payload: dict = {} if args is None else {"params": args}
    async with Client(server) as client:
        result = await client.call_tool(tool, payload)
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def test_standalone_remember_then_search(tmp_path: Path) -> None:
    db = tmp_path / "engrama.db"
    server = _make_server(db)

    remembered = await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "solo-note", "summary": "only-user note"}},
    )
    assert remembered.get("status") == "ok"

    hits = await _call(server, "engrama_search", {"query": "solo-note"})
    # Standalone single-user resolves a stable sub for both write and read,
    # so the node is visible to its own scope.
    assert isinstance(hits, dict), f"expected results dict, got {hits!r}"
    names = {h["name"] for h in hits.get("results", [])}
    assert "solo-note" in names
