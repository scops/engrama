"""Tests for ``dry_run`` on ``engrama_sync_vault`` / ``engrama_sync_note`` (#52 Phase D).

Drives the FastMCP server in-process via ``fastmcp.Client`` against the
SQLite backend. Each test owns a fresh vault + DB so the dry-run /
real-run pairs can be compared without cross-test interference.

Coverage:
* dry-run does not create any graph nodes
* dry-run does not modify any ``.md`` frontmatter on disk
* dry-run reports ``would_*`` counts and the list of files that would
  receive ``engrama_id``
* a subsequent real run still works after the dry run (the dry run
  isn't accidentally caching anything)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engrama.adapters.mcp.server import create_engrama_mcp

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
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
    """Three Concept-typed notes; none carry an engrama_id yet."""
    v = tmp_path / "vault"
    v.mkdir()
    for name in ("alpha", "beta", "gamma"):
        (v / f"{name}.md").write_text(
            f"---\ntype: Concept\nname: {name}\n---\n\n# {name}\n",
            encoding="utf-8",
        )
    return v


async def _call(server, tool: str, args: dict | None = None) -> dict:
    """Call a FastMCP tool in-process. Tool handlers in ``server.py``
    take a single Pydantic-model parameter named ``params``, so call
    arguments are nested under that key; for parameterless tools
    (``engrama_status``) pass ``None``."""
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


# ----------------------------------------------------------------------
# engrama_sync_vault dry_run
# ----------------------------------------------------------------------


async def test_sync_vault_dry_run_writes_nothing(tmp_path: Path, vault: Path) -> None:
    """Counts are projected and graph stays empty."""
    db = tmp_path / "engrama.db"
    server = _make_server(db, vault)

    note_paths = {p.name: p.read_text(encoding="utf-8") for p in vault.glob("*.md")}

    resp = await _call(server, "engrama_sync_vault", {"dry_run": True})
    status = await _call(server, "engrama_status")

    assert resp["status"] == "ok"
    assert resp["dry_run"] is True
    assert resp["would_create"] == 3
    assert resp["would_update"] == 0
    assert resp["would_inject_engrama_id"] == 3
    assert sorted(resp["files_would_receive_engrama_id"]) == [
        "alpha.md",
        "beta.md",
        "gamma.md",
    ]

    # Graph stayed empty.
    assert status["backend"]["node_count"] == 0

    # Note files on disk are byte-for-byte identical.
    for name, original in note_paths.items():
        assert (vault / name).read_text(encoding="utf-8") == original


async def test_sync_vault_real_run_after_dry_run(tmp_path: Path, vault: Path) -> None:
    """The dry-run mustn't leave caching/state that breaks a follow-up real run."""
    db = tmp_path / "engrama.db"
    server = _make_server(db, vault)

    dry = await _call(server, "engrama_sync_vault", {"dry_run": True})
    real = await _call(server, "engrama_sync_vault", {"dry_run": False})

    assert dry["would_create"] == 3
    assert real["dry_run"] is False
    assert real["created"] == 3
    assert real["updated"] == 0

    # After the real run, every note got an engrama_id injection.
    for name in ("alpha", "beta", "gamma"):
        assert "engrama_id:" in (vault / f"{name}.md").read_text(encoding="utf-8")


async def test_sync_vault_dry_run_distinguishes_create_vs_update(
    tmp_path: Path, vault: Path
) -> None:
    """After one real run, a second dry run reports ``would_update`` instead of
    ``would_create`` for the same notes."""
    db = tmp_path / "engrama.db"
    server = _make_server(db, vault)

    await _call(server, "engrama_sync_vault", {"dry_run": False})
    dry = await _call(server, "engrama_sync_vault", {"dry_run": True})

    assert dry["would_create"] == 0
    assert dry["would_update"] == 3
    # Notes already carry engrama_id, so the dry run wouldn't inject again.
    assert dry["would_inject_engrama_id"] == 0
    assert dry["files_would_receive_engrama_id"] == []


# ----------------------------------------------------------------------
# engrama_sync_note dry_run
# ----------------------------------------------------------------------


async def test_sync_note_dry_run_predicts_create_and_injection(tmp_path: Path, vault: Path) -> None:
    db = tmp_path / "engrama.db"
    server = _make_server(db, vault)

    original = (vault / "alpha.md").read_text(encoding="utf-8")
    resp = await _call(
        server,
        "engrama_sync_note",
        {"path": "alpha.md", "dry_run": True},
    )
    status = await _call(server, "engrama_status")

    assert resp["status"] == "ok"
    assert resp["dry_run"] is True
    assert resp["label"] == "Concept"
    assert resp["name"] == "alpha"
    assert resp["would_create"] is True
    assert resp["would_inject_engrama_id"] is True
    # Returns a candidate engrama_id but no write happened.
    assert resp["engrama_id"]

    assert status["backend"]["node_count"] == 0
    assert (vault / "alpha.md").read_text(encoding="utf-8") == original


async def test_sync_note_dry_run_after_existing_node(tmp_path: Path, vault: Path) -> None:
    """Once a node exists, dry-run should switch to ``would_create: False``."""
    db = tmp_path / "engrama.db"
    server = _make_server(db, vault)

    await _call(server, "engrama_sync_note", {"path": "alpha.md", "dry_run": False})
    resp = await _call(
        server,
        "engrama_sync_note",
        {"path": "alpha.md", "dry_run": True},
    )

    assert resp["would_create"] is False
    # Frontmatter was injected on the real run; second dry pass would not.
    assert resp["would_inject_engrama_id"] is False


async def test_sync_note_real_run_envelope_has_dry_run_false(tmp_path: Path, vault: Path) -> None:
    """Even on a real run, the response advertises ``dry_run: False`` so
    every caller can rely on the same envelope key being present."""
    db = tmp_path / "engrama.db"
    server = _make_server(db, vault)

    resp = await _call(server, "engrama_sync_note", {"path": "alpha.md"})

    assert resp["dry_run"] is False
    assert resp["created"] is True
    assert "node" in resp
