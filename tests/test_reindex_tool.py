"""Tests for the engrama_reindex tool (detect / classify / apply).

Reproduces the recovery flow for nodes that lost their vector (embedder down
at write time), and verifies the three phases. SQLite backend, in-process, no
network — never touches the shared Neo4j.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import engrama.backends as backends
from engrama.adapters.mcp.server import create_engrama_mcp

pytestmark = pytest.mark.asyncio

_VEC = [0.1] * 768


class _Embedder:
    """Configured embedder (dims=768); reachability driven by a shared flag."""

    def __init__(self, flag: dict) -> None:
        self.dimensions = 768
        self._flag = flag

    async def aembed(self, text: str):
        if not self._flag["up"]:
            raise ConnectionError("embedder unreachable (simulated)")
        return list(_VEC)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    for var in ("ENGRAMA_ORG_ID", "ENGRAMA_USER_ID", "ENGRAMA_AGENT_ID", "VAULT_PATH"):
        monkeypatch.delenv(var, raising=False)


async def _call(server, tool: str, args: dict) -> dict:
    from fastmcp import Client

    async with Client(server) as client:
        result = await client.call_tool(tool, {"params": args})
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def test_reindex_detect_classify_apply_heals_unembedded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = {"up": False}
    monkeypatch.setattr(backends, "create_embedding_provider", lambda *a, **k: _Embedder(flag))
    db = tmp_path / "engrama.db"
    server = create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)}, vault_path=None
    )

    # Two writes while the embedder is down → both persist without a vector.
    for name in ("alpha", "beta"):
        r = await _call(
            server,
            "engrama_remember",
            {"label": "Concept", "properties": {"name": name, "summary": f"about {name}"}},
        )
        assert r.get("embedded") is False

    # detect — read-only, finds both.
    detect = await _call(server, "engrama_reindex", {"mode": "detect"})
    assert detect["unembedded_found"] == 2

    # classify — both have text → re-embeddable.
    classify = await _call(server, "engrama_reindex", {"mode": "classify"})
    assert classify["to_reembed"] == 2
    assert classify["skip_no_text"] == 0

    # apply with default dry_run=true → simulates, writes nothing.
    dry = await _call(server, "engrama_reindex", {"mode": "apply"})
    assert dry["dry_run"] is True
    assert dry["would_reembed"] == 2
    still = await _call(server, "engrama_reindex", {"mode": "detect"})
    assert still["unembedded_found"] == 2  # unchanged

    # Embedder comes up; apply for real.
    flag["up"] = True
    applied = await _call(server, "engrama_reindex", {"mode": "apply", "dry_run": False})
    assert applied["reembedded"] == 2
    assert applied["failed"] == 0

    # Fully healed for this batch.
    healed = await _call(server, "engrama_reindex", {"mode": "detect"})
    assert healed["unembedded_found"] == 0


async def test_reindex_apply_without_embedder_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply on a no-embedder deployment returns a clear error, not a crash."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")  # NullProvider, dimensions=0
    server = create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(tmp_path / "engrama.db")}, vault_path=None
    )
    await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "x", "summary": "y"}},
    )
    resp = await _call(server, "engrama_reindex", {"mode": "apply", "dry_run": False})
    assert "error" in resp
    assert "embedder" in resp["error"].lower()


async def test_reindex_invalid_mode(tmp_path: Path) -> None:
    server = create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(tmp_path / "engrama.db")}, vault_path=None
    )
    from fastmcp import Client

    async with Client(server) as client:
        result = await client.call_tool("engrama_reindex", {"params": {"mode": "bogus"}})
        text = result.content[0].text  # type: ignore[union-attr]
    assert "invalid mode" in text.lower()
