"""MCP search payload exposes the spec-002 ranking signals (US3 T025).

The ``engrama_search`` hybrid response must surface ``rrf_score`` and
``graph_distance_score`` alongside the existing per-signal scores, so a
caller can explain any ranking decision from the payload alone (SC-006).

Driving the hybrid path needs an embedder with ``dimensions > 0``; a
deterministic fake is injected by patching the provider factory the server
lifespan calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


class _FakeEmbedder:
    """Deterministic offline embedder (constant vector) — dims=4."""

    dimensions = 4

    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    async def aembed_batch(self, texts: list[str]) -> list[list[float]]:
        return self.embed_batch(texts)

    def health_check(self) -> bool:
        return True

    async def ahealth_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    # The factory is patched below, so EMBEDDING_PROVIDER is irrelevant; clear
    # any inbound identity so standalone scope resolves.
    for var in (
        "ENGRAMA_ORG_ID",
        "ENGRAMA_USER_ID",
        "ENGRAMA_AGENT_ID",
        "ENGRAMA_SESSION_ID",
        "ENGRAMA_LOCAL_SUB",
        "ENGRAMA_RANKING_LEGACY",
    ):
        monkeypatch.delenv(var, raising=False)
    # Pin VAULT_PATH to a throwaway dir so a remember never touches a real
    # Obsidian vault configured via the dev .env (conftest runs load_dotenv).
    throwaway_vault = tmp_path_factory.mktemp("vault")
    monkeypatch.setenv("VAULT_PATH", str(throwaway_vault))
    # Inject the fake embedder wherever the lifespan resolves it.
    monkeypatch.setattr(
        "engrama.backends.create_embedding_provider",
        lambda *a, **k: _FakeEmbedder(),
    )


def _make_server(db: Path):
    from engrama.adapters.mcp.server import create_engrama_mcp

    return create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(db), "EMBEDDING_DIMENSIONS": "4"},
        vault_path=None,
    )


async def _call(server, tool: str, args: dict | None = None):
    from fastmcp import Client

    payload: dict = {} if args is None else {"params": args}
    async with Client(server) as client:
        result = await client.call_tool(tool, payload)
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def test_search_payload_exposes_rrf_and_graph_distance(tmp_path: Path) -> None:
    db = tmp_path / "engrama.db"
    server = _make_server(db)

    remembered = await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "alpha-node", "summary": "alpha topic"}},
    )
    assert remembered.get("status") == "ok"

    hits = await _call(server, "engrama_search", {"query": "alpha-node"})
    assert isinstance(hits, dict), f"expected results dict, got {hits!r}"
    results = hits.get("results", [])
    assert results, f"expected at least one hit, got {hits!r}"

    top = results[0]
    # Spec-002 ranking signals must be present alongside the existing scores.
    assert "rrf_score" in top
    assert "graph_distance_score" in top
    assert "vector_score" in top and "fulltext_score" in top
