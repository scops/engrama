"""Tests for the ``engrama_status`` MCP introspection tool (#52 Phase C).

Drives the FastMCP server in-process via the ``fastmcp.Client`` against
the SQLite backend, so the suite needs no external services. Asserts
the response shape, vault path identification (the contract that lets
agents disambiguate Engrama's vault from an external ``obsidian-mcp``
vault), and the embedder fallback when ``EMBEDDING_PROVIDER=null``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engrama.adapters.mcp.server import create_engrama_mcp

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Disable embeddings and drop any scope env so each test starts clean."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in (
        "ENGRAMA_ORG_ID",
        "ENGRAMA_USER_ID",
        "ENGRAMA_AGENT_ID",
        "ENGRAMA_SESSION_ID",
        "VAULT_PATH",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """A minimal Engrama vault with three notes."""
    v = tmp_path / "engrama-vault"
    v.mkdir()
    for name in ("alpha.md", "beta.md", "gamma.md"):
        (v / name).write_text(f"# {name.removesuffix('.md')}\n", encoding="utf-8")
    return v


async def _call_status(server) -> dict:
    """Invoke ``engrama_status`` in-process and parse the JSON payload."""
    from fastmcp import Client

    async with Client(server) as client:
        result = await client.call_tool("engrama_status", {})
        # FastMCP wraps the tool's string return as a TextContent block.
        text = result.content[0].text  # type: ignore[union-attr]
        return json.loads(text)


async def test_status_reports_vault_path_and_note_count(tmp_path: Path, vault: Path) -> None:
    """The vault path is the contract — it lets agents distinguish
    Engrama's vault from an external obsidian-mcp vault before any
    sync call."""
    server = create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(tmp_path / "engrama.db")},
        vault_path=str(vault),
    )
    payload = await _call_status(server)

    assert payload["vault"]["configured"] is True
    # Resolve both sides so we don't trip on a trailing-slash difference.
    assert Path(payload["vault"]["path"]) == vault.resolve()
    assert payload["vault"]["note_count"] == 3


async def test_status_handles_missing_vault(tmp_path: Path) -> None:
    """When no VAULT_PATH is configured the response says so explicitly
    instead of pretending a vault exists."""
    server = create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(tmp_path / "engrama.db")},
        vault_path=None,
    )
    payload = await _call_status(server)

    assert payload["vault"]["configured"] is False
    assert "path" not in payload["vault"]
    assert "note_count" not in payload["vault"]


async def test_status_reports_backend_and_version(tmp_path: Path) -> None:
    """Backend identity + engrama version are the other two signals
    agents need to reason about an installation."""
    server = create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(tmp_path / "engrama.db")},
        vault_path=None,
    )
    payload = await _call_status(server)

    assert payload["backend"]["name"] == "sqlite"
    assert payload["backend"]["ok"] is True
    assert payload["backend"].get("node_count", 0) == 0  # fresh DB

    from engrama import __version__

    assert payload["version"] == __version__


async def test_status_search_mode_fulltext_only_when_embedder_null(
    tmp_path: Path,
) -> None:
    """With ``EMBEDDING_PROVIDER=null`` the embedder has dimensions=0,
    so the next engrama_search would be fulltext-only. The status tool
    must surface that *before* a search is run, so agents don't expect
    hybrid ranking when it isn't available."""
    server = create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(tmp_path / "engrama.db")},
        vault_path=None,
    )
    payload = await _call_status(server)

    assert payload["embedder"]["configured"] is True
    assert payload["embedder"]["provider"] == "none"
    assert payload["embedder"]["dimensions"] == 0
    assert payload["search"]["mode"] == "fulltext_only"
    assert payload["search"]["degraded"] is False
    assert payload["search"]["reason"]  # non-empty


async def test_status_is_read_only(tmp_path: Path) -> None:
    """Calling engrama_status twice in a row returns the same node_count
    — proves it doesn't mutate the graph as a side effect."""
    server = create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(tmp_path / "engrama.db")},
        vault_path=None,
    )
    first = await _call_status(server)
    second = await _call_status(server)
    assert first["backend"]["node_count"] == second["backend"]["node_count"]
