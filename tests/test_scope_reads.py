"""Read-side scope filter tests (Spec 001 — fail-closed tenancy).

Covers the helpers `scope_filter_sql` / `scope_filter_cypher`, plus
SQLite-backed integration tests that exercise the fail-closed visibility
rule end-to-end: a resolved scope sees only its own ``(org_id, user_id)``
nodes plus the org-shared ``__entity__`` sentinel — no inheritance, no
foreign-org or global bleed. A ``None``/empty scope at the helper level is
the system/admin path (no filter); request-boundary fail-closed is enforced
by the MCP resolver.
"""

from __future__ import annotations

import pytest

from engrama.backends.sqlite import SqliteGraphStore
from engrama.core.scope import MemoryScope, scope_filter_cypher, scope_filter_sql

# ---------------------------------------------------------------------------
# 1. Filter helpers — return shapes
# ---------------------------------------------------------------------------


class TestScopeFilterHelpers:
    def test_sql_none_scope_matches_nothing(self):
        # Hard fail-closed (Spec 001 FR-5): no scope is a bug, never "see all".
        assert scope_filter_sql(None, "n") == ("(1 = 0)", {})

    def test_sql_empty_scope_matches_nothing(self):
        assert scope_filter_sql(MemoryScope(), "n") == ("(1 = 0)", {})

    def test_sql_incomplete_scope_matches_nothing(self):
        # Only one of the (org_id, user_id) pair → incomplete → match nothing.
        assert scope_filter_sql(MemoryScope(user_id="alice"), "n") == ("(1 = 0)", {})
        assert scope_filter_sql(MemoryScope(org_id="acme"), "n") == ("(1 = 0)", {})

    def test_sql_complete_scope(self):
        # The (org_id, user_id) pair; user_id predicate includes the
        # __entity__ org-shared sentinel.
        clause, params = scope_filter_sql(MemoryScope(user_id="alice", org_id="acme"), "n")
        assert clause == (
            "(n.org_id = :scope_org_id AND n.user_id IN (:scope_user_id, :scope_entity))"
        )
        assert params == {
            "scope_org_id": "acme",
            "scope_user_id": "alice",
            "scope_entity": "__entity__",
        }

    def test_sql_ignores_agent_and_session(self):
        # agent_id/session_id are provenance, never part of the filter (R-1).
        clause, params = scope_filter_sql(
            MemoryScope(org_id="acme", user_id="alice", agent_id="bot", session_id="conv"), "n"
        )
        assert "agent_id" not in clause
        assert "session_id" not in clause
        assert set(params) == {"scope_org_id", "scope_user_id", "scope_entity"}

    def test_sql_with_json_column(self):
        clause, _ = scope_filter_sql(
            MemoryScope(org_id="acme", user_id="alice"), "n", json_column="props"
        )
        assert clause == (
            "(json_extract(n.props, '$.org_id') = :scope_org_id"
            " AND json_extract(n.props, '$.user_id') IN (:scope_user_id, :scope_entity))"
        )

    def test_cypher_none_scope_matches_nothing(self):
        assert scope_filter_cypher(None, "node") == ("(false)", {})

    def test_cypher_incomplete_scope_matches_nothing(self):
        assert scope_filter_cypher(MemoryScope(user_id="alice"), "node") == ("(false)", {})

    def test_cypher_complete_scope(self):
        clause, params = scope_filter_cypher(MemoryScope(user_id="alice", org_id="acme"), "node")
        assert clause == (
            "(node.org_id = $scope_org_id AND node.user_id IN [$scope_user_id, $scope_entity])"
        )
        assert params == {
            "scope_org_id": "acme",
            "scope_user_id": "alice",
            "scope_entity": "__entity__",
        }

    @pytest.mark.parametrize(
        "bad",
        ["n.evil", "n; DROP TABLE nodes; --", "1node", "", "n)"],
    )
    def test_sql_rejects_non_identifier_alias(self, bad):
        with pytest.raises(ValueError, match="table_alias"):
            scope_filter_sql(MemoryScope(user_id="alice"), bad)

    @pytest.mark.parametrize("bad", ["props.evil", "1col", "props'"])
    def test_sql_rejects_non_identifier_json_column(self, bad):
        with pytest.raises(ValueError, match="json_column"):
            scope_filter_sql(MemoryScope(user_id="alice"), "n", json_column=bad)

    def test_sql_validates_identifiers_even_for_empty_scope(self):
        # Identifier validation runs before the scope-empty short-circuit
        # so callers can't accidentally pass tainted strings on the
        # "no-op" path either.
        with pytest.raises(ValueError):
            scope_filter_sql(None, "bad alias")

    @pytest.mark.parametrize(
        "bad",
        ["node.evil", "node; MATCH (x) DELETE x; //", "1node", ""],
    )
    def test_cypher_rejects_non_identifier_node_var(self, bad):
        with pytest.raises(ValueError, match="node_var"):
            scope_filter_cypher(MemoryScope(user_id="alice"), bad)


# ---------------------------------------------------------------------------
# 2. End-to-end SQLite integration — visibility rule on fulltext_search
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "scope-reads.db")
    yield s
    s.close()


def _seed(store: SqliteGraphStore) -> None:
    """Three nodes: alice's, the shared org's, and bob's."""
    store.merge_node(
        "Concept",
        "name",
        "alice_secret",
        {"notes": "alice private memo", "user_id": "alice", "org_id": "acme"},
    )
    store.merge_node(
        "Concept",
        "name",
        "org_shared",
        {"notes": "shared acme handbook", "org_id": "acme"},
    )
    store.merge_node(
        "Concept",
        "name",
        "bob_secret",
        {"notes": "bob private memo", "user_id": "bob", "org_id": "acme"},
    )
    store.merge_node(
        "Concept",
        "name",
        "public_memo",
        {"notes": "public global thing"},
    )


class TestSqliteFulltextScope:
    def test_no_scope_sees_nothing(self, store):
        # Hard fail-closed: a read with no scope is a bug → zero rows, never all.
        _seed(store)
        names = {r["name"] for r in store.fulltext_search("memo OR handbook OR thing")}
        assert names == set()

    def test_alice_sees_only_her_own(self, store):
        _seed(store)
        scope = MemoryScope(user_id="alice", org_id="acme")
        names = {r["name"] for r in store.fulltext_search("memo OR handbook OR thing", scope=scope)}
        # Fail-closed: no inheritance. Visible only: alice_secret. NOT visible:
        # org_shared (no user_id), public_memo (no scope), bob_secret (other user).
        assert names == {"alice_secret"}
        assert "bob_secret" not in names
        assert "org_shared" not in names

    def test_other_org_isolated(self, store):
        _seed(store)
        scope = MemoryScope(org_id="other_corp")
        names = {r["name"] for r in store.fulltext_search("memo OR handbook OR thing", scope=scope)}
        # A foreign org sees nothing seeded (no fail-open inheritance to global).
        assert names == set()

    def test_empty_scope_matches_nothing(self, store):
        _seed(store)
        names = {
            r["name"]
            for r in store.fulltext_search("memo OR handbook OR thing", scope=MemoryScope())
        }
        assert names == set()


# ---------------------------------------------------------------------------
# 3. End-to-end SQLite integration — visibility rule on get_neighbours
# ---------------------------------------------------------------------------


class TestSqliteGetNeighboursScope:
    def test_alice_cannot_traverse_to_bobs_node(self, store):
        _seed(store)
        # Wire a relation between alice_secret and bob_secret. Alice
        # should see her own node but not jump to bob's via traversal.
        store.merge_relation(
            "Concept", "name", "alice_secret", "RELATED_TO", "Concept", "name", "bob_secret"
        )
        scope = MemoryScope(user_id="alice", org_id="acme")
        rows = store.get_neighbours("Concept", "name", "alice_secret", scope=scope)
        neighbour_names = {row["neighbour"]["name"] for row in rows}
        assert "bob_secret" not in neighbour_names

    def test_bobs_node_invisible_as_start(self, store):
        _seed(store)
        scope = MemoryScope(user_id="alice", org_id="acme")
        # Trying to traverse FROM bob's node returns empty for alice.
        rows = store.get_neighbours("Concept", "name", "bob_secret", scope=scope)
        assert rows == []

    def test_no_scope_traverses_nothing(self, store):
        # Hard fail-closed: traversal with no scope returns no start node.
        _seed(store)
        store.merge_relation(
            "Concept", "name", "alice_secret", "RELATED_TO", "Concept", "name", "bob_secret"
        )
        rows = store.get_neighbours("Concept", "name", "alice_secret")
        assert rows == []
