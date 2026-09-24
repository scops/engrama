"""Regression suite — MCP tool output never carries a node's vector embedding.

Incident (2026-09-21): on Neo4j the embedding is an ordinary node property
(the vector index reads it from there), so ``engrama_remember`` updating an
existing node echoed ``node.embedding`` back to the caller — the whole vector,
thousands of tokens the model cannot use. Creating a node did not leak only
because the vector is written *after* the MERGE whose row the response echoes.
``engrama_sync_note`` had the same path; ``engrama_context`` only stayed clean
because each backend stripped the field on its own.

The fix is one output sanitiser (``sanitize_node_for_output``) applied at the
MCP boundary to every node the tools return. These tests pin it:

* a Neo4j-shaped store (SQLite wrapped so read-backs carry the vector, exactly
  as Neo4j's ``dict(record["n"])`` does) exercises every node-returning tool
  on any machine, no Neo4j needed;
* the same create → update → read flow against the real backends,
  parametrised over SQLite and Neo4j (Neo4j skips without ``NEO4J_PASSWORD``).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

import engrama.backends as backends
from engrama.adapters.mcp.server import create_engrama_mcp
from engrama.backends.sqlite.async_store import SqliteAsyncStore

_DIMS = 768

# Fields that must never appear in a tool response, at any depth. Spelled out
# here (not imported from the sanitiser) so the suite also catches a sanitiser
# whose own list regresses.
_NEVER_IN_OUTPUT = {"embedding"}


class _Embedder:
    """Always-up embedder with the dimensionality the Neo4j schema expects."""

    dimensions = _DIMS

    async def aembed(self, text: str) -> list[float]:
        return [0.0123] * _DIMS


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", str(_DIMS))
    for var in ("ENGRAMA_ORG_ID", "ENGRAMA_USER_ID", "ENGRAMA_AGENT_ID", "VAULT_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(backends, "create_embedding_provider", lambda *a, **k: _Embedder())


def _keys_anywhere(obj: Any) -> set[str]:
    """Every dict key in a decoded JSON payload, at any depth."""
    if isinstance(obj, dict):
        keys = set(obj)
        for v in obj.values():
            keys |= _keys_anywhere(v)
        return keys
    if isinstance(obj, list):
        return set().union(*(_keys_anywhere(v) for v in obj)) if obj else set()
    return set()


def _assert_clean(payload: dict, tool: str) -> None:
    leaked = _keys_anywhere(payload) & _NEVER_IN_OUTPUT
    assert not leaked, f"{tool} response leaked internal field(s): {sorted(leaked)}"


async def _call(client, tool: str, args: dict) -> dict:
    result = await client.call_tool(tool, {"params": args})
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


# --- sanitiser unit ---------------------------------------------------------


def test_sanitize_node_for_output_drops_internal_fields_only() -> None:
    from engrama.core.security import sanitize_node_for_output

    node = {
        "name": "n",
        "summary": "s",
        "embedding": [0.1, 0.2],
        "_id": 7,
        "_labels": ["Concept"],
        "embedded": True,
    }
    out = sanitize_node_for_output(node)
    assert out == {"name": "n", "summary": "s", "embedded": True}
    assert "embedding" in node, "the caller's dict must not be mutated"
    assert sanitize_node_for_output(None) == {}


# --- Neo4j-shaped store: every node-returning tool --------------------------


@pytest.fixture
def neo4j_shaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make SQLite read-backs carry the vector the way Neo4j's do.

    SQLite keeps vectors in a separate ``vec0`` table, so its node dicts are
    naturally clean — which is exactly why the leak went unnoticed. Neo4j
    stores the vector as ``n.embedding``; any ``RETURN n`` after it was set
    includes it. Mirror that: once a node has a vector, ``merge_node`` and
    ``get_node_with_neighbours`` hand it back on the node dict.
    """
    vectors: dict[tuple[str, str], list[float]] = {}
    orig_store = SqliteAsyncStore.store_embedding
    orig_merge = SqliteAsyncStore.merge_node
    orig_ctx = SqliteAsyncStore.get_node_with_neighbours

    async def store_embedding(self, label, key_field, key_value, embedding, owner=None):
        vectors[(label, key_value)] = list(embedding)
        return await orig_store(self, label, key_field, key_value, embedding, owner=owner)

    async def merge_node(self, label, key_field, key_value, properties, embedding=None):
        result = await orig_merge(self, label, key_field, key_value, properties, embedding)
        vec = vectors.get((label, key_value))
        if vec is not None and result.get("node"):
            result["node"]["embedding"] = vec
        return result

    async def get_node_with_neighbours(self, label, key_field, key_value, hops=1, scope=None):
        data = await orig_ctx(self, label, key_field, key_value, hops, scope)
        if data:
            vec = vectors.get((label, key_value))
            if vec is not None:
                data["node"]["embedding"] = vec
            for nb in data["neighbours"]:
                nb_vec = vectors.get((nb["label"], nb["name"]))
                if nb_vec is not None:
                    nb["properties"]["embedding"] = nb_vec
        return data

    monkeypatch.setattr(SqliteAsyncStore, "store_embedding", store_embedding)
    monkeypatch.setattr(SqliteAsyncStore, "merge_node", merge_node)
    monkeypatch.setattr(SqliteAsyncStore, "get_node_with_neighbours", get_node_with_neighbours)


async def test_remember_update_does_not_return_embedding(tmp_path: Path, neo4j_shaped) -> None:
    from mcp.client import Client

    server = create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(tmp_path / "e.db")}, vault_path=None
    )
    async with Client(server) as client:
        props = {"name": "leak-probe", "summary": "v1"}
        created = await _call(
            client, "engrama_remember", {"label": "Material", "properties": props}
        )
        assert created["status"] == "ok" and created["embedded"] is True
        _assert_clean(created, "engrama_remember (create)")

        props = {"name": "leak-probe", "summary": "v2", "details": "updated by MERGE"}
        updated = await _call(
            client, "engrama_remember", {"label": "Material", "properties": props}
        )
        assert updated["status"] == "ok"
        assert updated["node"]["summary"] == "v2", "the rest of the node must still come back"
        _assert_clean(updated, "engrama_remember (update)")


async def test_sync_note_update_does_not_return_embedding(tmp_path: Path, neo4j_shaped) -> None:
    from mcp.client import Client

    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "leak-probe.md"
    note.write_text("---\ntype: Concept\nname: leak-probe\nsummary: v1\n---\n\n# leak-probe\n")

    server = create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(tmp_path / "e.db")}, vault_path=str(vault)
    )
    async with Client(server) as client:
        first = await _call(client, "engrama_sync_note", {"path": "leak-probe.md"})
        assert first["status"] == "ok" and first["created"] is True
        _assert_clean(first, "engrama_sync_note (create)")

        second = await _call(client, "engrama_sync_note", {"path": "leak-probe.md"})
        assert second["status"] == "ok" and second["created"] is False
        assert second["node"]["name"] == "leak-probe"
        _assert_clean(second, "engrama_sync_note (update)")


async def test_context_does_not_return_embedding(tmp_path: Path, neo4j_shaped) -> None:
    """Root and neighbours are sanitised at the MCP boundary, not left to
    whatever the backend happens to strip."""
    from mcp.client import Client

    server = create_engrama_mcp(
        backend="sqlite", config={"ENGRAMA_DB_PATH": str(tmp_path / "e.db")}, vault_path=None
    )
    async with Client(server) as client:
        for name in ("ctx-root", "ctx-neighbour"):
            await _call(
                client,
                "engrama_remember",
                {"label": "Concept", "properties": {"name": name, "summary": name}},
            )
        rel = await _call(
            client,
            "engrama_relate",
            {
                "from_name": "ctx-root",
                "from_label": "Concept",
                "rel_type": "RELATED_TO",
                "to_name": "ctx-neighbour",
                "to_label": "Concept",
            },
        )
        assert rel["status"] == "ok"
        _assert_clean(rel, "engrama_relate")

        ctx = await _call(client, "engrama_context", {"name": "ctx-root", "label": "Concept"})
        assert ctx["node"]["name"] == "ctx-root"
        assert [n["name"] for n in ctx["neighbours"]] == ["ctx-neighbour"]
        _assert_clean(ctx, "engrama_context")


# --- real backends: SQLite and Neo4j ----------------------------------------


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("neo4j", marks=pytest.mark.neo4j),
    ]
)
def live_server(request: pytest.FixtureRequest, tmp_path: Path):
    """An MCP server on a real backend plus a unique name prefix; Neo4j test
    nodes are removed afterwards so the shared graph stays clean."""
    prefix = f"embleak-{uuid.uuid4().hex[:8]}"
    if request.param == "sqlite":
        yield (
            create_engrama_mcp(
                backend="sqlite",
                config={"ENGRAMA_DB_PATH": str(tmp_path / "e.db")},
                vault_path=None,
            ),
            prefix,
        )
        return

    driver = request.getfixturevalue("neo4j_driver")
    cfg = {
        "NEO4J_URI": os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        "NEO4J_USERNAME": os.getenv("NEO4J_USERNAME", "neo4j"),
        "NEO4J_PASSWORD": os.getenv("NEO4J_PASSWORD", ""),
    }
    # The standalone identity sub is persisted next to ENGRAMA_DB_PATH; keep
    # it inside tmp_path so the test never touches the developer's own.
    cfg["ENGRAMA_DB_PATH"] = str(tmp_path / "unused.db")
    try:
        yield create_engrama_mcp(backend="neo4j", config=cfg, vault_path=None), prefix
    finally:
        with driver.session() as session:
            session.run(
                "MATCH (n) WHERE coalesce(n.name, n.title) STARTS WITH $prefix DETACH DELETE n",
                prefix=prefix,
            )


async def test_live_backend_tools_never_return_embedding(live_server) -> None:
    from mcp.client import Client

    server, prefix = live_server
    root, other = f"{prefix}-root", f"{prefix}-other"
    async with Client(server) as client:
        for tool, args in (
            (
                "engrama_remember",
                {"label": "Material", "properties": {"name": root, "summary": "v1"}},
            ),
            (
                "engrama_remember",
                {"label": "Material", "properties": {"name": other, "summary": "o"}},
            ),
            # The update is the path that leaked: the vector is on the node now.
            (
                "engrama_remember",
                {"label": "Material", "properties": {"name": root, "summary": "v2"}},
            ),
            (
                "engrama_relate",
                {
                    "from_name": root,
                    "from_label": "Material",
                    "rel_type": "RELATED_TO",
                    "to_name": other,
                    "to_label": "Material",
                },
            ),
            ("engrama_context", {"name": root, "label": "Material"}),
            ("engrama_search", {"query": prefix}),
            # Read-only tools that project stored nodes; cheap to sweep here.
            ("engrama_surface_insights", {}),
            ("engrama_status", None),
        ):
            result = await client.call_tool(tool, {} if args is None else {"params": args})
            text = result.content[0].text  # type: ignore[union-attr]
            if tool == "engrama_search" and not text.lstrip().startswith("{"):
                continue  # "No results found" — nothing to leak
            payload = json.loads(text)
            assert payload.get("status", "ok") == "ok", (tool, payload)
            _assert_clean(payload, tool)
