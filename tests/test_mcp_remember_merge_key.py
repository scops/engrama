"""MCP-layer regression for ``TITLE_KEYED_LABELS`` canonicalisation.

#53 fixed ``EngramaEngine.merge_node`` so SDK callers that put the wrong
key in the property bag (``name`` for a title-keyed label, or vice
versa) still merge onto the canonical row. The MCP ``engrama_remember``
handler bypasses the engine and writes to the async store directly, so
the fix didn't reach that path — agents that sent
``{"label": "Experiment", "properties": {"name": ...}}`` were silently
creating rows keyed by ``name`` and diverging from SDK writes.

These tests exercise the handler in-process via ``fastmcp.Client``
against the SQLite backend and pin the canonicalisation at the MCP
boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engrama.adapters.mcp.server import create_engrama_mcp
from engrama.core.schema import TITLE_KEYED_LABELS

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


async def _call(server, tool: str, args: dict | None = None) -> dict:
    from fastmcp import Client

    payload: dict = {} if args is None else {"params": args}
    async with Client(server) as client:
        result = await client.call_tool(tool, payload)
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


def _make_server(db: Path):
    return create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(db)},
        vault_path=None,
    )


# ``Insight`` is title-keyed and reserved for the reflect skill; the
# MCP ``engrama_remember`` handler does not write Insights directly,
# so leaving it out keeps the parametrisation focused on the
# user-callable subset.
_USER_TITLE_LABELS = sorted(TITLE_KEYED_LABELS - {"Insight"})


@pytest.mark.parametrize("label", _USER_TITLE_LABELS)
async def test_remember_canonicalises_name_to_title(tmp_path: Path, label: str) -> None:
    """An MCP caller passing ``name`` for a title-keyed label must land
    on a row keyed by ``title``."""
    identity = f"mcp-name-{label.lower()}"
    server = _make_server(tmp_path / "engrama.db")

    resp = await _call(
        server,
        "engrama_remember",
        {"label": label, "properties": {"name": identity, "notes": "via MCP"}},
    )

    assert "Error" not in resp.get("status", "") if isinstance(resp.get("status"), str) else True

    # Read the stored node via the engine path — it's the authoritative
    # check, and it works on either backend.
    from engrama import Engrama

    with Engrama(backend="sqlite", db_path=tmp_path / "engrama.db") as eng:
        node = eng._store.get_node(label, "title", identity) or {}
        assert node, f"node not found by canonical 'title' for {label}"
        assert node.get("title") == identity
        # The non-canonical alias must NOT have been persisted.
        assert "name" not in node or node.get("name") != identity


async def test_remember_canonicalises_title_to_name_on_concept(tmp_path: Path) -> None:
    """Symmetric direction: ``Concept`` is name-keyed, so a stray
    ``title`` in the bag must be demoted to ``name``."""
    server = _make_server(tmp_path / "engrama.db")

    await _call(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"title": "demote-me", "notes": "wrong key"}},
    )

    from engrama import Engrama

    with Engrama(backend="sqlite", db_path=tmp_path / "engrama.db") as eng:
        node = eng._store.get_node("Concept", "name", "demote-me") or {}
        assert node
        assert node.get("name") == "demote-me"
        assert "title" not in node or node.get("title") != "demote-me"


async def test_remember_with_both_keys_keeps_canonical(tmp_path: Path) -> None:
    """When both ``name`` and ``title`` are present for a title-keyed
    label, the canonical value wins and the alias is silently dropped
    — matching the engine + Sanitiser pattern."""
    server = _make_server(tmp_path / "engrama.db")

    await _call(
        server,
        "engrama_remember",
        {
            "label": "Experiment",
            "properties": {
                "title": "canonical-wins",
                "name": "loser",
                "notes": "conflict",
            },
        },
    )

    from engrama import Engrama

    with Engrama(backend="sqlite", db_path=tmp_path / "engrama.db") as eng:
        winner = eng._store.get_node("Experiment", "title", "canonical-wins") or {}
        loser_row = eng._store.get_node("Experiment", "title", "loser")

        assert winner
        assert winner.get("title") == "canonical-wins"
        assert winner.get("notes") == "conflict"
        assert loser_row is None


async def test_remember_missing_key_returns_label_specific_error(tmp_path: Path) -> None:
    """The error message names the canonical key the label expects, so
    callers debug the right thing instead of guessing."""
    server = _make_server(tmp_path / "engrama.db")

    resp_str_experiment = await _call_raw(
        _make_server(tmp_path / "engrama2.db"),
        "engrama_remember",
        {"label": "Experiment", "properties": {"notes": "no identity"}},
    )
    assert "'title'" in resp_str_experiment
    assert "'Experiment'" in resp_str_experiment

    resp_str_concept = await _call_raw(
        server,
        "engrama_remember",
        {"label": "Concept", "properties": {"notes": "no identity"}},
    )
    assert "'name'" in resp_str_concept
    assert "'Concept'" in resp_str_concept


async def _call_raw(server, tool: str, args: dict) -> str:
    """Return the raw tool response text (the handler emits a string error,
    not JSON, when the validation fails)."""
    from fastmcp import Client

    async with Client(server) as client:
        result = await client.call_tool(tool, {"params": args})
        return result.content[0].text  # type: ignore[union-attr]
