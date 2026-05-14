"""Read-side scope filter tests (DDR-003 Phase F / Roadmap P14).

Covers the helpers `scope_filter_sql` / `scope_filter_cypher`, plus
SQLite-backed integration tests that exercise the visibility rule
end-to-end: alice sees her own + shared-org + global, never sees
bob's, scope=None returns everything.
"""

from __future__ import annotations

import pytest

from engrama.backends.sqlite import SqliteGraphStore
from engrama.core.scope import MemoryScope, scope_filter_cypher, scope_filter_sql

# ---------------------------------------------------------------------------
# 1. Filter helpers — return shapes
# ---------------------------------------------------------------------------


class TestScopeFilterHelpers:
    def test_sql_none_scope_returns_empty(self):
        assert scope_filter_sql(None, "n") == ("", {})

    def test_sql_empty_scope_returns_empty(self):
        assert scope_filter_sql(MemoryScope(), "n") == ("", {})

    def test_sql_single_dimension(self):
        clause, params = scope_filter_sql(MemoryScope(user_id="alice"), "n")
        assert clause == "(n.user_id IS NULL OR n.user_id = :scope_user_id)"
        assert params == {"scope_user_id": "alice"}

    def test_sql_multiple_dimensions(self):
        clause, params = scope_filter_sql(MemoryScope(user_id="alice", org_id="acme"), "n")
        # Dimension order is org → user → agent → session.
        assert clause == (
            "(n.org_id IS NULL OR n.org_id = :scope_org_id)"
            " AND (n.user_id IS NULL OR n.user_id = :scope_user_id)"
        )
        assert params == {"scope_org_id": "acme", "scope_user_id": "alice"}

    def test_sql_with_json_column(self):
        clause, _ = scope_filter_sql(MemoryScope(user_id="alice"), "n", json_column="props")
        assert clause == (
            "(json_extract(n.props, '$.user_id') IS NULL"
            " OR json_extract(n.props, '$.user_id') = :scope_user_id)"
        )

    def test_cypher_none_scope_returns_empty(self):
        assert scope_filter_cypher(None, "node") == ("", {})

    def test_cypher_single_dimension(self):
        clause, params = scope_filter_cypher(MemoryScope(user_id="alice"), "node")
        assert clause == "(node.user_id IS NULL OR node.user_id = $scope_user_id)"
        assert params == {"scope_user_id": "alice"}


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
    def test_no_scope_sees_everything(self, store):
        _seed(store)
        names = {r["name"] for r in store.fulltext_search("memo OR handbook OR thing")}
        assert names == {"alice_secret", "org_shared", "bob_secret", "public_memo"}

    def test_alice_sees_own_org_and_global(self, store):
        _seed(store)
        scope = MemoryScope(user_id="alice", org_id="acme")
        names = {r["name"] for r in store.fulltext_search("memo OR handbook OR thing", scope=scope)}
        # Visible: alice_secret (matches user), org_shared (no user_id),
        # public_memo (no scope at all). Not visible: bob_secret.
        assert names == {"alice_secret", "org_shared", "public_memo"}
        assert "bob_secret" not in names

    def test_other_org_isolated(self, store):
        _seed(store)
        scope = MemoryScope(org_id="other_corp")
        names = {r["name"] for r in store.fulltext_search("memo OR handbook OR thing", scope=scope)}
        # Only the unscoped "public_memo" is visible to a foreign org.
        assert names == {"public_memo"}

    def test_empty_scope_acts_as_no_filter(self, store):
        _seed(store)
        names = {
            r["name"]
            for r in store.fulltext_search("memo OR handbook OR thing", scope=MemoryScope())
        }
        assert names == {"alice_secret", "org_shared", "bob_secret", "public_memo"}


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

    def test_no_scope_traverses_freely(self, store):
        _seed(store)
        store.merge_relation(
            "Concept", "name", "alice_secret", "RELATED_TO", "Concept", "name", "bob_secret"
        )
        rows = store.get_neighbours("Concept", "name", "alice_secret")
        neighbour_names = {row["neighbour"]["name"] for row in rows}
        assert "bob_secret" in neighbour_names
