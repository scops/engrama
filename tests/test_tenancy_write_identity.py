"""Write-identity enforcement tests (Spec 001, T010 / T-4 / FR-1, FR-4).

Three layers of fail-closed write enforcement:

1. **Engine defence-in-depth (T011)** — :meth:`EngramaEngine.merge_node`
   refuses to persist a node when the effective scope is missing or
   incomplete. The MCP boundary already rejects unresolved identity, so this
   layer catches SDK / future-call bypasses.
2. **MCP boundary (T012)** — every write tool already calls
   :func:`resolve_scope` and converts :class:`ScopeUnresolved` into an
   explicit error response. This file pins the contract by simulating an
   unresolved request (no headers, no standalone sub) and asserting each
   write tool returns a status-error payload, never touches the graph.
3. **Relation stamping (T012a, FR-1)** — :meth:`GraphStore.merge_relation`
   persists ``(org_id, user_id)`` on the edge itself, so a future query
   that joins through an edge can be filtered without re-walking the
   endpoint nodes.

NFR: every test runs under ``EMBEDDING_PROVIDER=null`` and uses SQLite so
the suite is local-safe (no Neo4j writes against the shared prod graph).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engrama.backends.sqlite import SqliteGraphStore
from engrama.core.engine import EngramaEngine
from engrama.core.scope import MemoryScope, ScopeIncomplete

# ---------------------------------------------------------------------------
# Hermetic env
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Engine-level defence-in-depth (T011)
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> SqliteGraphStore:
    s = SqliteGraphStore(tmp_path / "writes.db")
    yield s
    s.close()


class TestEngineMergeNodeRefusesIncompleteScope:
    """``EngramaEngine.merge_node`` is fail-closed on the write path.

    The MCP layer is the *primary* boundary that rejects unresolved
    identity; this engine guard is the *secondary* boundary that catches
    a direct SDK/legacy call that bypasses MCP.
    """

    def test_no_default_scope_rejects_write(self, store: SqliteGraphStore) -> None:
        engine = EngramaEngine(store)  # no default_scope
        with pytest.raises(ScopeIncomplete):
            engine.merge_node("Concept", {"name": "leak-me"})
        # And the graph must remain untouched (defence-in-depth holds even
        # when the test runs adjacent to other writes).
        assert store.get_node("Concept", "name", "leak-me") is None

    def test_empty_default_scope_rejects_write(self, store: SqliteGraphStore) -> None:
        engine = EngramaEngine(store, default_scope=MemoryScope())
        with pytest.raises(ScopeIncomplete):
            engine.merge_node("Concept", {"name": "leak-me"})
        assert store.get_node("Concept", "name", "leak-me") is None

    def test_partial_org_only_rejects_write(self, store: SqliteGraphStore) -> None:
        engine = EngramaEngine(store, default_scope=MemoryScope(org_id="acme"))
        with pytest.raises(ScopeIncomplete):
            engine.merge_node("Concept", {"name": "leak-me"})
        assert store.get_node("Concept", "name", "leak-me") is None

    def test_partial_user_only_rejects_write(self, store: SqliteGraphStore) -> None:
        engine = EngramaEngine(store, default_scope=MemoryScope(user_id="alice"))
        with pytest.raises(ScopeIncomplete):
            engine.merge_node("Concept", {"name": "leak-me"})
        assert store.get_node("Concept", "name", "leak-me") is None

    def test_explicit_scope_kwarg_with_missing_user_rejects(
        self, store: SqliteGraphStore
    ) -> None:
        # default_scope is fine, but an explicit kwarg override that is
        # incomplete must still fail — the explicit arg wins.
        engine = EngramaEngine(
            store, default_scope=MemoryScope(org_id="acme", user_id="alice")
        )
        with pytest.raises(ScopeIncomplete):
            engine.merge_node(
                "Concept",
                {"name": "leak-me"},
                scope=MemoryScope(org_id="acme"),  # incomplete override
            )
        assert store.get_node("Concept", "name", "leak-me") is None

    def test_complete_scope_succeeds(self, store: SqliteGraphStore) -> None:
        engine = EngramaEngine(
            store, default_scope=MemoryScope(org_id="acme", user_id="alice")
        )
        engine.merge_node("Concept", {"name": "kept"})
        node = store.get_node("Concept", "name", "kept")
        assert node is not None
        assert node.get("org_id") == "acme"
        assert node.get("user_id") == "alice"

    def test_entity_sentinel_user_id_succeeds(self, store: SqliteGraphStore) -> None:
        # Org-shared promotion writes use ``user_id="__entity__"``; the
        # engine accepts this because the spec allows it as a real value
        # for the user_id dimension (FR-8).
        engine = EngramaEngine(
            store, default_scope=MemoryScope(org_id="acme", user_id="__entity__")
        )
        engine.merge_node("Concept", {"name": "shared-org-asset"})
        node = store.get_node("Concept", "name", "shared-org-asset")
        assert node is not None
        assert node.get("user_id") == "__entity__"


# ---------------------------------------------------------------------------
# Relations carry scope (T012a / FR-1)
# ---------------------------------------------------------------------------


def _edge_props(store: SqliteGraphStore, from_value: str, to_value: str) -> dict:
    """Fetch the ``edges`` row joining two nodes by ``key_value``."""
    cur = store._conn.execute(
        """
        SELECT e.* FROM edges e
        JOIN nodes f ON f.id = e.from_id
        JOIN nodes t ON t.id = e.to_id
        WHERE f.key_value = ? AND t.key_value = ?
        """,
        (from_value, to_value),
    )
    row = cur.fetchone()
    return dict(row) if row else {}


class TestRelationCarriesScope:
    """Every relation written through ``GraphStore.merge_relation`` persists
    the writer's ``(org_id, user_id)`` on the edge so a future query that
    routes through an edge can be filtered without re-walking endpoints.
    """

    def test_sqlite_relation_stamped_with_scope(self, store: SqliteGraphStore) -> None:
        engine = EngramaEngine(
            store, default_scope=MemoryScope(org_id="acme", user_id="alice")
        )
        engine.merge_node("Project", {"name": "p"})
        engine.merge_node("Technology", {"name": "t"})
        engine.merge_relation("p", "Project", "USES", "t", "Technology")
        props = _edge_props(store, "p", "t")
        assert props, "expected edge row p -[USES]-> t"
        assert props.get("org_id") == "acme"
        assert props.get("user_id") == "alice"

    def test_sqlite_relation_rejected_when_scope_incomplete(
        self, store: SqliteGraphStore
    ) -> None:
        # An incomplete scope on the writer must not let a relation slip
        # through — defence-in-depth at the engine layer also covers
        # ``merge_relation``.
        engine = EngramaEngine(store, default_scope=MemoryScope(org_id="acme"))
        # Pre-populate the endpoints unscoped so we can isolate the
        # relation-write check (we drop straight to the store to bypass
        # the engine's node-write guard, since we're testing the relation
        # path here).
        store.merge_node("Project", "name", "p", {"org_id": "acme", "user_id": "alice"})
        store.merge_node("Technology", "name", "t", {"org_id": "acme", "user_id": "alice"})
        with pytest.raises(ScopeIncomplete):
            engine.merge_relation("p", "Project", "USES", "t", "Technology")
        # No edge written.
        assert _edge_props(store, "p", "t") == {}


# ---------------------------------------------------------------------------
# MCP boundary (T012) — every write tool rejects ScopeUnresolved
# ---------------------------------------------------------------------------
#
# We monkeypatch :func:`engrama.adapters.mcp.server.resolve_scope` to raise
# :class:`ScopeUnresolved` and invoke each write tool via
# :class:`fastmcp.Client` against an in-process MCP server. Every tool must
# convert the exception into a ``{"status": "error", ...}`` payload and
# leave the graph untouched.


_WRITE_TOOL_INVOCATIONS = [
    pytest.param(
        "engrama_remember",
        {
            "label": "Concept",
            "properties": {"name": "leak-target", "notes": "should not write"},
        },
        ("Concept", "name", "leak-target"),
        id="engrama_remember",
    ),
    pytest.param(
        "engrama_relate",
        {
            "from_name": "a",
            "from_label": "Concept",
            "rel_type": "RELATED_TO",
            "to_name": "b",
            "to_label": "Concept",
        },
        None,
        id="engrama_relate",
    ),
    pytest.param("engrama_reflect", {}, None, id="engrama_reflect"),
    pytest.param(
        "engrama_approve_insight",
        {"title": "x", "action": "approve"},
        None,
        id="engrama_approve_insight",
    ),
    pytest.param(
        "engrama_write_insight_to_vault",
        {"title": "x", "target_note": "x.md"},
        None,
        id="engrama_write_insight_to_vault",
    ),
    pytest.param(
        "engrama_ingest",
        {"source_type": "text", "source": "hello world"},
        None,
        id="engrama_ingest",
    ),
    pytest.param(
        "engrama_sync_note",
        {"path": "x.md"},
        None,
        id="engrama_sync_note",
    ),
    pytest.param(
        "engrama_sync_vault",
        {},
        None,
        id="engrama_sync_vault",
    ),
    pytest.param(
        "engrama_reindex",
        {"mode": "detect"},
        None,
        id="engrama_reindex",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,args,touch_assert", _WRITE_TOOL_INVOCATIONS)
async def test_mcp_write_tool_rejects_unresolved_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    args: dict,
    touch_assert,
) -> None:
    """End-to-end through ``fastmcp.Client``: when the resolver raises
    ``ScopeUnresolved``, the tool must surface ``status:"error"`` and the
    graph must remain untouched.
    """
    from fastmcp import Client

    from engrama.adapters.mcp import server as srv_module
    from engrama.adapters.mcp.server import ScopeUnresolved, create_engrama_mcp

    db = tmp_path / "boundary.db"
    server = create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(db)},
        vault_path=None,
    )

    def _raise(_ctx):
        raise ScopeUnresolved("test: missing identity")

    monkeypatch.setattr(srv_module, "resolve_scope", _raise)

    async with Client(server) as client:
        result = await client.call_tool(tool_name, {"params": args})
        text = result.content[0].text  # type: ignore[union-attr]
    import json

    payload = json.loads(text)
    assert isinstance(payload, dict)
    assert payload.get("status") == "error", (
        f"{tool_name} did not surface ScopeUnresolved as status:error — payload={payload}"
    )

    # Graph must remain untouched: the relevant node (when applicable) is
    # absent. ``touch_assert`` is (label, key_field, key_value) when the
    # tool would otherwise have written a node we can address by key.
    if touch_assert is not None:
        label, key_field, key_value = touch_assert
        store = SqliteGraphStore(db)
        try:
            assert store.get_node(label, key_field, key_value) is None, (
                f"{tool_name} wrote a node despite ScopeUnresolved: "
                f"({label!r}, {key_field}={key_value!r})"
            )
        finally:
            store.close()
