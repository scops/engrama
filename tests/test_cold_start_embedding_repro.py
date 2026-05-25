"""Regression suite — embedder failures on write are surfaced, not silent.

Incident (2026-05-25): when the embedder was configured but unreachable on a
write (a remote embeddings endpoint cold-starting), ``engrama_remember``
persisted the node without a vector and still returned ``status: "ok"`` with no
signal — the node became permanently invisible to semantic search.

These tests pin the fix:
* a failed embed surfaces ``embedded: false`` + an ``embedding_note`` (honest,
  not silent), while the node still persists (proactive writes are never lost);
* a healthy write embeds and reports ``embedded: true``;
* the opportunistic sweep heals a previously-pending node on the next write
  whose own embed succeeded (live proof the embedder is reachable).

SQLite backend, in-process, no network — never touches the shared Neo4j.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import engrama.backends as backends
import engrama.backends.sqlite.async_store as sqlite_async
import engrama.backends.sqlite.store as sqlite_sync
from engrama.adapters.mcp.server import create_engrama_mcp

pytestmark = pytest.mark.asyncio

_VEC = [0.1] * 768


class _Embedder:
    """Configured embedder (dims=768) whose reachability is driven by a shared
    flag, so a test can simulate the embedder being down and then up."""

    def __init__(self, flag: dict) -> None:
        self.dimensions = 768
        self._flag = flag

    async def aembed(self, text: str):
        if not self._flag["up"]:
            raise ConnectionError("embedder unreachable (simulated cold-start)")
        return list(_VEC)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    for var in ("ENGRAMA_ORG_ID", "ENGRAMA_USER_ID", "ENGRAMA_AGENT_ID", "VAULT_PATH"):
        monkeypatch.delenv(var, raising=False)


def _use_embedder(monkeypatch: pytest.MonkeyPatch, flag: dict) -> None:
    monkeypatch.setattr(backends, "create_embedding_provider", lambda *a, **k: _Embedder(flag))


def _server(db: Path):
    return create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)}, vault_path=None
    )


async def _call(server, tool: str, args: dict) -> dict:
    from fastmcp import Client

    async with Client(server) as client:
        result = await client.call_tool(tool, {"params": args})
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def test_embed_failure_is_surfaced_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_embedder(monkeypatch, {"up": False})

    calls = {"store_embedding": 0}
    orig = sqlite_async.SqliteAsyncStore.store_embedding

    async def _spy(self, *a, **k):
        calls["store_embedding"] += 1
        return await orig(self, *a, **k)

    monkeypatch.setattr(sqlite_async.SqliteAsyncStore, "store_embedding", _spy)

    server = _server(tmp_path / "engrama.db")
    resp = await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "cold-node", "summary": "off-chain scaling"}},
    )

    # Node persisted and reported success (proactive writes are never lost)...
    assert resp.get("status") == "ok"
    assert resp.get("engrama_id")
    # ...but the dropped vector is now HONEST, not silent.
    assert resp.get("embedded") is False
    assert "embedding_note" in resp
    assert calls["store_embedding"] == 0


async def test_healthy_write_embeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_embedder(monkeypatch, {"up": True})
    server = _server(tmp_path / "engrama.db")

    resp = await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "warm-node", "summary": "channels"}},
    )

    assert resp.get("embedded") is True
    assert "embedding_note" not in resp


async def test_opportunistic_sweep_heals_prior_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = {"up": False}
    _use_embedder(monkeypatch, flag)
    db = tmp_path / "engrama.db"
    server = _server(db)

    # Write 1 — embedder down → node persisted without a vector.
    r1 = await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "pending-node", "summary": "off-chain"}},
    )
    assert r1.get("embedded") is False

    # Embedder comes up. Write 2 embeds itself AND, because that proves the
    # embedder is reachable, sweeps the prior pending node.
    flag["up"] = True
    r2 = await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "healthy-node", "summary": "lightning"}},
    )
    assert r2.get("embedded") is True

    # pending-node must no longer be vector-less.
    store = sqlite_sync.SqliteGraphStore(str(db))
    remaining = {c["key_value"] for c in store.list_unembedded_nodes()}
    assert "pending-node" not in remaining, (
        f"sweep should have healed it; still unembedded: {remaining}"
    )
