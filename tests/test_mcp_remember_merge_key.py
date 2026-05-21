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


# ---------------------------------------------------------------------------
# Inline relations — target merge key must follow TITLE_KEYED_LABELS too
# ---------------------------------------------------------------------------
#
# v0.11.0 canonicalised the SOURCE merge key (#51 / #53) but the inline
# ``relations={...}`` path in ``engrama_remember`` was still calling
# ``store.merge_relation`` with a hardcoded ``"name"`` for the target
# merge key. For title-keyed targets (Decision, Problem, Experiment,
# Vulnerability, Exercise, Photo) the underlying Cypher resolved to
# ``MATCH (b:Experiment {name: $value})``, matched zero rows, and the
# MERGE created no edge — but ``relations_created`` still incremented
# because no exception was raised. The bug was silent end-to-end.
#
# These tests pin the fix: existing title-keyed targets must be reached
# via their canonical key, and the response counter must reflect what
# actually landed in the graph.


async def test_inline_relation_resolves_title_keyed_target(tmp_path: Path) -> None:
    """Inline relation to a pre-existing title-keyed node lands an edge."""
    db = tmp_path / "engrama.db"
    server = _make_server(db)

    # Seed a title-keyed target node first.
    seed = await _call(
        server,
        "engrama_remember",
        {
            "label": "Decision",
            "properties": {
                "title": "adopt-canonical-keys",
                "notes": "target for the inline-relation test",
            },
        },
    )
    assert seed.get("status") == "ok"

    # Now create a name-keyed source with an inline relation pointing
    # at the title-keyed target by its identity. Before the fix this
    # MERGE found nothing and the counter lied.
    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {
                "name": "canonical-keys-policy",
                "notes": "source pointing at a title-keyed target",
                "relations": {"INFORMED_BY": ["adopt-canonical-keys"]},
            },
        },
    )

    assert resp.get("status") == "ok"
    assert resp.get("relations_created") == 1, (
        f"counter must reflect the edge that actually landed; got {resp.get('relations_created')!r}"
    )

    # The edge must be traversable from the source's neighbourhood,
    # and it must point at the canonical (title-keyed) row — not at
    # a stray ``name``-keyed stub.
    from engrama import Engrama

    with Engrama(backend="sqlite", db_path=db) as eng:
        nb = eng._store.get_neighbours("Concept", "name", "canonical-keys-policy")
        targets = [
            (
                n["neighbour"]["_labels"][0],
                n["neighbour"].get("title") or n["neighbour"].get("name"),
            )
            for n in nb
        ]
        assert ("Decision", "adopt-canonical-keys") in targets

        # And no spurious ``Decision`` row was created under ``name``.
        stub = eng._store.get_node("Decision", "name", "adopt-canonical-keys")
        assert stub is None or stub.get("name") != "adopt-canonical-keys"


async def test_inline_relation_counter_does_not_lie_on_missed_match(
    tmp_path: Path,
) -> None:
    """If the target genuinely cannot be matched, the counter must NOT
    increment.

    This guards against the original regression mode where
    ``merge_relation`` returned an empty dict and the handler counted
    it as success anyway. Here the target is a stub label that the
    inferer creates under ``name`` — so the MERGE itself succeeds and
    the counter legitimately reads 1. The test below targets the
    other shape: the COUNTER must agree with the graph.
    """
    db = tmp_path / "engrama.db"
    server = _make_server(db)

    resp = await _call(
        server,
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {
                "name": "isolated-concept",
                "notes": "target will be a fresh stub",
                "relations": {"RELATED_TO": ["brand-new-target"]},
            },
        },
    )

    assert resp.get("status") == "ok"
    counter = resp.get("relations_created", 0)

    from engrama import Engrama

    with Engrama(backend="sqlite", db_path=db) as eng:
        nb = eng._store.get_neighbours("Concept", "name", "isolated-concept")
        assert counter == len(nb), (
            "relations_created must equal the number of edges actually "
            f"present in the graph: counter={counter}, edges={len(nb)}"
        )
