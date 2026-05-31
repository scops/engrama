"""Tests for the ``engrama_gdpr_forget`` MCP tool (Spec 001 US-3 / T031).

Drives the FastMCP server in-process via ``fastmcp.Client`` against the
SQLite backend. With no ``X-Engrama-*`` headers the server resolves the
standalone single-user identity, so a node written by ``engrama_remember``
and the erasure target share one scope.

DESTRUCTIVE-CODE SAFETY (non-negotiable): every test owns a fresh vault +
DB under ``tmp_path``. The shared Neo4j (production) is never touched.
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
        "ENGRAMA_LOCAL_SUB",
        "VAULT_PATH",
    ):
        monkeypatch.delenv(var, raising=False)


async def _call(server, tool: str, args: dict | None = None) -> dict:
    from fastmcp import Client

    payload: dict = {} if args is None else {"params": args}
    async with Client(server) as client:
        result = await client.call_tool(tool, payload)
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


def _make_server(db: Path, vault: Path):
    return create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(db)},
        vault_path=str(vault),
    )


@pytest.fixture()
def server(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return _make_server(tmp_path / "gdpr.db", vault)


async def _seed_two_nodes(server) -> None:
    await _call(server, "engrama_remember", {"label": "Project", "properties": {"name": "proj-x"}})
    await _call(
        server, "engrama_remember", {"label": "Technology", "properties": {"name": "tech-y"}}
    )


async def test_invalid_mode_is_rejected(server) -> None:
    out = await _call(server, "engrama_gdpr_forget", {"mode": "nuke"})
    assert out["status"] == "error"
    assert "mode" in out["message"]


async def test_dry_run_reports_without_deleting(server) -> None:
    await _seed_two_nodes(server)

    first = await _call(server, "engrama_gdpr_forget", {"mode": "dry-run"})
    assert first["mode"] == "dry-run"
    assert first["deleted_nodes_by_label"] == {"Project": 1, "Technology": 1}
    assert "timestamp" in first and "org_id" in first and "user_id" in first

    # A second dry-run sees the very same graph → nothing was deleted.
    second = await _call(server, "engrama_gdpr_forget", {"mode": "dry-run"})
    assert second["deleted_nodes_by_label"] == {"Project": 1, "Technology": 1}


async def test_default_mode_is_dry_run(server) -> None:
    await _seed_two_nodes(server)
    out = await _call(server, "engrama_gdpr_forget", {})
    assert out["mode"] == "dry-run"
    # Still present after the default call (safe default).
    again = await _call(server, "engrama_gdpr_forget", {"mode": "dry-run"})
    assert again["deleted_nodes_by_label"] == {"Project": 1, "Technology": 1}


async def test_apply_erases_then_is_idempotent(server) -> None:
    await _seed_two_nodes(server)

    applied = await _call(server, "engrama_gdpr_forget", {"mode": "apply"})
    assert applied["mode"] == "apply"
    assert applied["deleted_nodes_by_label"] == {"Project": 1, "Technology": 1}

    again = await _call(server, "engrama_gdpr_forget", {"mode": "apply"})
    assert again["deleted_nodes_by_label"] == {}
    assert again["deleted_relations"] == 0
    assert again["deleted_embeddings"] == 0
