"""Fail-closed scope contract tests (Spec 001, T002 / FR-2, FR-8).

These pin the inverted, fail-closed behaviour of the scope-filter helpers and
the ``__entity__`` org-shared sentinel, end-to-end on SQLite. They are the
specification of intended behaviour for the scope-core inversion (T003–T005).
"""

from __future__ import annotations

import pytest

from engrama.backends.sqlite import SqliteGraphStore
from engrama.core.scope import (
    ENTITY_SENTINEL,
    MemoryScope,
    scope_filter_cypher,
    scope_filter_sql,
)


class TestFailClosedHelpers:
    def test_sentinel_value(self):
        assert ENTITY_SENTINEL == "__entity__"

    def test_no_is_null_or_branch(self):
        # The fail-open inheritance branch must be gone (FR-2).
        clause, _ = scope_filter_sql(MemoryScope(org_id="acme", user_id="alice"), "n")
        assert "IS NULL" not in clause
        cypher, _ = scope_filter_cypher(MemoryScope(org_id="acme", user_id="alice"), "node")
        assert "IS NULL" not in cypher

    def test_user_predicate_includes_entity_sentinel(self):
        _, params = scope_filter_cypher(MemoryScope(org_id="acme", user_id="alice"), "node")
        assert params["scope_entity"] == ENTITY_SENTINEL

    def test_agent_session_not_filtered(self):
        clause, params = scope_filter_cypher(
            MemoryScope(org_id="acme", user_id="alice", agent_id="bot", session_id="s"), "node"
        )
        assert "agent_id" not in clause and "session_id" not in clause
        assert set(params) == {"scope_org_id", "scope_user_id", "scope_entity"}


@pytest.fixture()
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "failclosed.db")
    yield s
    s.close()


def _seed(store: SqliteGraphStore) -> None:
    store.merge_node(
        "Concept",
        "name",
        "alice_secret",
        {"notes": "alice private memo", "user_id": "alice", "org_id": "acme"},
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
        "org_handbook",
        {"notes": "shared acme handbook", "user_id": ENTITY_SENTINEL, "org_id": "acme"},
    )
    store.merge_node(
        "Concept",
        "name",
        "other_org_secret",
        {"notes": "memo elsewhere", "user_id": "carol", "org_id": "globex"},
    )


class TestFailClosedVisibility:
    def test_user_sees_own_and_entity_shared_only(self, store):
        _seed(store)
        scope = MemoryScope(org_id="acme", user_id="alice")
        names = {r["name"] for r in store.fulltext_search("memo OR handbook", scope=scope)}
        # Own node + the org-shared __entity__ node; never bob's, never globex.
        assert names == {"alice_secret", "org_handbook"}

    def test_entity_node_visible_across_users_same_org(self, store):
        _seed(store)
        bob_scope = MemoryScope(org_id="acme", user_id="bob")
        names = {r["name"] for r in store.fulltext_search("handbook", scope=bob_scope)}
        assert "org_handbook" in names

    def test_entity_node_not_visible_to_other_org(self, store):
        _seed(store)
        globex = MemoryScope(org_id="globex", user_id="carol")
        names = {r["name"] for r in store.fulltext_search("handbook", scope=globex)}
        assert "org_handbook" not in names

    def test_no_cross_org_user_collision(self, store):
        # Same user_id string under a different org must not bleed across orgs.
        store.merge_node(
            "Concept",
            "name",
            "acme_alice",
            {"notes": "memo one", "user_id": "alice", "org_id": "acme"},
        )
        store.merge_node(
            "Concept",
            "name",
            "globex_alice",
            {"notes": "memo two", "user_id": "alice", "org_id": "globex"},
        )
        scope = MemoryScope(org_id="acme", user_id="alice")
        names = {r["name"] for r in store.fulltext_search("memo", scope=scope)}
        assert "globex_alice" not in names
        assert "acme_alice" in names
