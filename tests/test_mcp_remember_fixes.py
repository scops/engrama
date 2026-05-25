"""MCP ``engrama_remember`` regressions from the SaaS-pod testing round.

Covers four fixes surfaced while exercising the headless (no-vault,
fulltext-only) deployment:

* #6 — every node gets a stable ``engrama_id`` minted storage-native, so
  the response contract holds even without a vault, and it never changes
  on update.
* #5 — a caller's *semantic* ``origin`` is preserved, while ``source``
  stays the system-managed transport bucket (``"mcp"``) and cannot be
  spoofed.
* #3 — inline relations accept an explicit ``{"name", "label"}`` object
  to pin a stub's label, falling back to relation-type inference for bare
  strings (and for invalid labels).

All run in-process against the SQLite backend (no Neo4j, no network).
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


def _make_server(db: Path):
    return create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(db)},
        vault_path=None,
    )


async def _call(server, tool: str, args: dict | None = None) -> dict:
    from fastmcp import Client

    payload: dict = {} if args is None else {"params": args}
    async with Client(server) as client:
        result = await client.call_tool(tool, payload)
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


# --- #6: stable engrama_id, no vault required ------------------------------


async def test_remember_returns_stable_engrama_id_without_vault(tmp_path: Path) -> None:
    """A no-vault remember still returns a non-null engrama_id, the node
    carries the same id, and re-remembering does not change it."""
    db = tmp_path / "engrama.db"
    server = _make_server(db)

    first = await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "stable-id", "summary": "v1"}},
    )
    assert first.get("status") == "ok"
    eid = first.get("engrama_id")
    assert eid, "engrama_id must be present and non-null without a vault"
    assert first["node"].get("engrama_id") == eid, "node id must match the response id"

    second = await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"name": "stable-id", "summary": "v2"}},
    )
    assert second.get("engrama_id") == eid, "engrama_id must be stable across updates"


# --- #5: semantic origin vs transport source -------------------------------


async def test_remember_preserves_origin_and_stamps_source(tmp_path: Path) -> None:
    """``origin`` (semantic) is preserved; ``source`` stays the transport
    bucket and cannot be set by the caller."""
    db = tmp_path / "engrama.db"
    server = _make_server(db)

    await _call(
        server,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {
                "name": "origin-vs-source",
                # The caller tries to set both. origin must survive; source
                # must be ignored in favour of the transport stamp.
                "origin": "conversation",
                "source": "conversation",
            },
        },
    )

    from engrama import Engrama

    with Engrama(backend="sqlite", db_path=db) as eng:
        node = eng._store.get_node("Concept", "name", "origin-vs-source") or {}
        assert node.get("origin") == "conversation", "semantic origin must be preserved"
        assert node.get("source") == "mcp", "source must be the system transport bucket"


# --- #3: explicit stub label in inline relations ---------------------------


async def test_inline_relation_explicit_label_pins_stub_label(tmp_path: Path) -> None:
    """An object target {name,label} creates the stub under the given
    label, overriding the lossy relation-type inference."""
    db = tmp_path / "engrama.db"
    server = _make_server(db)

    # RELATED_TO has no specific mapping → would infer Concept. The
    # explicit label must win and create a Tool instead.
    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {
                "name": "course-on-bitcoin",
                "relations": {"RELATED_TO": [{"name": "BDK", "label": "Tool"}]},
            },
        },
    )
    assert resp.get("status") == "ok"
    assert resp.get("relations_created") == 1

    from engrama import Engrama

    with Engrama(backend="sqlite", db_path=db) as eng:
        tool = eng._store.get_node("Tool", "name", "BDK")
        assert tool, "stub must be created under the explicit label (Tool)"
        # And NOT under the inferred fallback label.
        assert eng._store.get_node("Concept", "name", "BDK") is None


async def test_inline_relation_invalid_explicit_label_falls_back_to_inference(
    tmp_path: Path,
) -> None:
    """An unknown explicit label is ignored and the relation-type inference
    is used instead (no crash, no bogus label)."""
    db = tmp_path / "engrama.db"
    server = _make_server(db)

    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {
                "name": "fallback-source",
                "relations": {"RELATED_TO": [{"name": "Mystery", "label": "NotALabel"}]},
            },
        },
    )
    assert resp.get("status") == "ok"

    from engrama import Engrama

    with Engrama(backend="sqlite", db_path=db) as eng:
        # RELATED_TO falls back to Concept (the universal bridge).
        assert eng._store.get_node("Concept", "name", "Mystery"), (
            "invalid explicit label must fall back to inferred Concept"
        )


async def test_inline_relation_bare_string_still_supported(tmp_path: Path) -> None:
    """Backward compatibility: a plain string target still creates a stub
    via relation-type inference."""
    db = tmp_path / "engrama.db"
    server = _make_server(db)

    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {
                "name": "legacy-string-form",
                "relations": {"RELATED_TO": ["plain-target"]},
            },
        },
    )
    assert resp.get("status") == "ok"
    assert resp.get("relations_created") == 1

    from engrama import Engrama

    with Engrama(backend="sqlite", db_path=db) as eng:
        assert eng._store.get_node("Concept", "name", "plain-target")
