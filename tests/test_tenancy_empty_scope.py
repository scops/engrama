"""Empty-scope read tests (Spec 001, US-2 / T-2).

T-1 (cross-tenant isolation) tests that scope B never sees scope A's data
when *both* tenants have data. This file tests the stronger empty-scope
guarantee: a complete scope ``(org_id, user_id)`` with **no own data**
still returns 0 rows from every read path, even when the graph is full
of another tenant's nodes / Insights / relations.

The mechanism is the same fail-closed equality filter; the value of
covering each read path separately is that a future regression
(e.g. someone adds a new read method and forgets to thread scope) is
caught here.

NFR-5 (degraded-embedder-still-scoped): every test runs under
``EMBEDDING_PROVIDER=null`` so the SDK falls back to the fulltext path
on search. The empty-scope guarantee must hold for the degraded path too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engrama import Engrama
from engrama.core.scope import MemoryScope

# ---------------------------------------------------------------------------
# Hermetic env: null embedder + cleared scope env vars so MemoryScope.from_env
# can't leak the local user into the test scopes.
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


# Scope under test: complete identity, but writes nothing — used to read.
_EMPTY_SCOPE_KW = {"org_id": "acme-empty", "user_id": "alice-empty"}
_DATA_SCOPE_KW = {"org_id": "globex-rich", "user_id": "bob-rich"}


# ---------------------------------------------------------------------------
# Fixture: populate every read path with another tenant's data
# ---------------------------------------------------------------------------


@pytest.fixture()
def populated_db(tmp_path: Path) -> Path:
    """Populate a shared SQLite DB under ``_DATA_SCOPE_KW`` with one fact per
    read path: a Project, a relation, an Insight (pending + approved +
    dismissed), and the seven reflect detector patterns (cross-project,
    shared tech, training, technique transfer, concept cluster, stale
    knowledge, under-connected). The empty-scope tests below read the same
    DB under ``_EMPTY_SCOPE_KW`` and assert every read returns 0 rows.
    """
    db = tmp_path / "shared.db"
    with Engrama(backend="sqlite", db_path=db, **_DATA_SCOPE_KW) as eng:
        # --- Plain nodes + relation (drives search / context / lookup /
        #     list_existing_nodes) ---
        eng.remember("Project", "rich-proj", "bob's project notes about widgets")
        eng.remember("Technology", "rich-tech", "framework that powers rich-proj")
        eng.associate("rich-proj", "Project", "USES", "rich-tech", "Technology")

        # --- Insight nodes in every status (drives surface / approve /
        #     get_*_titles / find_insight_by_source_query) ---
        eng._store.merge_node(
            "Insight",
            "title",
            "rich-pending",
            {
                "body": "pending insight body",
                "confidence": 0.8,
                "status": "pending",
                "source_query": "manual",
                **_DATA_SCOPE_KW,
            },
        )
        eng._store.merge_node(
            "Insight",
            "title",
            "rich-approved",
            {
                "body": "approved insight body",
                "confidence": 0.7,
                "status": "approved",
                "source_query": "manual",
                **_DATA_SCOPE_KW,
            },
        )
        eng._store.merge_node(
            "Insight",
            "title",
            "rich-dismissed",
            {
                "body": "dismissed insight body",
                "confidence": 0.5,
                "status": "dismissed",
                "source_query": "under_connected",
                **_DATA_SCOPE_KW,
            },
        )

        # --- Reflect detect_* fixtures ---
        # Cross-project solution: alpha resolved a Problem via a Decision,
        # beta has an open Problem on the same Concept.
        eng.remember("Project", "alpha", "rich tenant alpha")
        eng.remember("Project", "beta", "rich tenant beta")
        eng._store.merge_node(
            "Problem", "title", "leak", {"status": "resolved", **_DATA_SCOPE_KW}
        )
        eng._store.merge_node(
            "Problem", "title", "exposed", {"status": "open", **_DATA_SCOPE_KW}
        )
        eng._store.merge_node("Decision", "title", "use-tls", _DATA_SCOPE_KW)
        eng.remember("Concept", "encryption", "shared concept")
        eng._store.merge_relation("Project", "name", "alpha", "HAS", "Problem", "title", "leak")
        eng._store.merge_relation(
            "Problem", "title", "leak", "INSTANCE_OF", "Concept", "name", "encryption"
        )
        eng._store.merge_relation(
            "Problem", "title", "leak", "SOLVED_BY", "Decision", "title", "use-tls"
        )
        eng._store.merge_relation(
            "Project", "name", "alpha", "INFORMED_BY", "Decision", "title", "use-tls"
        )
        eng._store.merge_relation("Project", "name", "beta", "HAS", "Problem", "title", "exposed")
        eng._store.merge_relation(
            "Problem", "title", "exposed", "APPLIES", "Concept", "name", "encryption"
        )

        # Shared technology: two projects use a Technology.
        eng._store.merge_relation(
            "Project", "name", "alpha", "USES", "Technology", "name", "rich-tech"
        )
        eng._store.merge_relation(
            "Project", "name", "beta", "USES", "Technology", "name", "rich-tech"
        )

        # Training opportunity: open Problem + Course share a Concept.
        eng.remember("Course", "secure-coding", "course about input validation")
        eng._store.merge_relation(
            "Course", "name", "secure-coding", "COVERS", "Concept", "name", "encryption"
        )

        # Concept cluster: at least three entities on a Concept.
        for proj in ("cluster-a", "cluster-b", "cluster-c", "cluster-d"):
            eng.remember("Project", proj, f"clustered {proj}")
            eng._store.merge_relation(
                "Project", "name", proj, "APPLIES", "Concept", "name", "encryption"
            )

        # Under-connected: a lonely node with zero edges.
        eng.remember("Technology", "lonely-tech", "no edges intentionally")
    return db


# ---------------------------------------------------------------------------
# Per-read-path empty-scope assertions
# ---------------------------------------------------------------------------


def test_empty_scope_search_returns_zero(populated_db: Path) -> None:
    """``Engrama.search`` (fulltext) under an empty scope sees nothing."""
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        hits = eng.search("widgets OR project OR notes OR framework", limit=50)
    assert hits == []


def test_empty_scope_recall_returns_zero(populated_db: Path) -> None:
    """``Engrama.recall`` (search + neighbour expansion) sees nothing."""
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        hits = eng.recall("widgets OR notes", limit=50, hops=2)
    assert hits == []


def test_empty_scope_count_labels_returns_empty(populated_db: Path) -> None:
    """``count_labels`` (drives the reflect graph profile) sees nothing."""
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        profile = eng._store.count_labels(scope=eng._engine.default_scope)
    assert profile == {}


def test_empty_scope_lookup_node_label_returns_none(populated_db: Path) -> None:
    """``lookup_node_label`` (drives engrama_relate target resolution)
    cannot find any of the other tenant's nodes.
    """
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        scope = eng._engine.default_scope
        for name in ("rich-proj", "rich-tech", "alpha", "beta", "encryption", "use-tls"):
            assert eng._store.lookup_node_label(name, scope=scope) is None, (
                f"lookup_node_label leaked for {name!r}"
            )


def test_empty_scope_list_existing_nodes_returns_empty(populated_db: Path) -> None:
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        rows = eng._store.list_existing_nodes(limit=200, scope=eng._engine.default_scope)
    assert rows == []


def test_empty_scope_get_neighbours_returns_empty(populated_db: Path) -> None:
    """``get_neighbours`` cannot traverse into another tenant's graph."""
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        rows = eng._store.get_neighbours(
            "Project", "name", "rich-proj", hops=2, scope=eng._engine.default_scope
        )
    # Either the root is invisible (zero rows) or — if the store returns a
    # row whose ``start`` is empty — the neighbour list is empty. Both are
    # acceptable, but a populated neighbour from the other tenant would be
    # the leak we forbid.
    leaked = [
        r for r in rows if r.get("neighbour") and r["neighbour"].get("name")
    ]
    assert leaked == []


def test_empty_scope_get_pending_insights_returns_empty(populated_db: Path) -> None:
    """``surface_insights`` (= get_pending_insights) cannot see the other
    tenant's pending Insight.
    """
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        assert eng.surface_insights(limit=50) == []


def test_empty_scope_get_insight_by_title_returns_none(populated_db: Path) -> None:
    """``approve_insight`` / ``write_insight_to_vault`` first read the
    Insight by title; the empty scope must see ``None`` for every title.
    """
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        scope = eng._engine.default_scope
        for title in ("rich-pending", "rich-approved", "rich-dismissed"):
            assert eng._store.get_insight_by_title(title, scope=scope) is None


def test_empty_scope_insight_title_sets_are_empty(populated_db: Path) -> None:
    """``get_dismissed_insight_titles`` / ``get_approved_insight_titles``
    (drive reflect's dedup) return empty for the empty scope.
    """
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        scope = eng._engine.default_scope
        assert eng._store.get_dismissed_insight_titles(scope=scope) == set()
        assert eng._store.get_approved_insight_titles(scope=scope) == set()


def test_empty_scope_find_insight_by_source_query_returns_none(populated_db: Path) -> None:
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        scope = eng._engine.default_scope
        assert (
            eng._store.find_insight_by_source_query(
                "under_connected", statuses=["dismissed"], scope=scope
            )
            is None
        )
        assert (
            eng._store.find_insight_by_source_query("manual", scope=scope) is None
        )


def test_empty_scope_reflect_detectors_return_empty(populated_db: Path) -> None:
    """All seven reflect detectors return [] for the empty scope, even
    though the populated tenant has every pattern wired up.
    """
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        scope = eng._engine.default_scope
        assert eng._store.detect_cross_project_solutions(scope=scope) == []
        assert eng._store.detect_shared_technology(scope=scope) == []
        assert eng._store.detect_training_opportunities(scope=scope) == []
        assert eng._store.detect_technique_transfer(scope=scope) == []
        assert eng._store.detect_concept_clusters(scope=scope) == []
        assert eng._store.detect_stale_knowledge(scope=scope) == []
        assert eng._store.detect_under_connected_nodes(scope=scope) == []


def test_empty_scope_reflect_run_writes_no_insights(populated_db: Path) -> None:
    """End-to-end: ``Engrama.reflect`` on the empty scope produces zero
    insights, even though the populated tenant has every pattern.
    """
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        insights = eng.reflect()
    assert insights == []


# ---------------------------------------------------------------------------
# NFR-5: degraded-embedder-still-scoped
# ---------------------------------------------------------------------------


def test_nfr5_degraded_embedder_keeps_scope_filter(populated_db: Path) -> None:
    """``EMBEDDING_PROVIDER=null`` makes vector search a no-op; the search
    falls back to fulltext. The fail-closed scope filter must still apply
    on that fallback path — otherwise a degraded deployment would silently
    leak across tenants.

    The autouse fixture above already pins ``EMBEDDING_PROVIDER=null``, so
    this test makes the contract explicit: ``hybrid_search`` (which prefers
    vector when available, falls back to fulltext) returns nothing for the
    empty scope under degraded conditions.
    """
    with Engrama(backend="sqlite", db_path=populated_db, **_EMPTY_SCOPE_KW) as eng:
        # Sanity: no embedder configured under the autouse env.
        assert eng._embedder is None or getattr(eng._embedder, "dimensions", 0) == 0
        # Both search shapes must come back empty.
        assert eng.search("widgets OR notes OR framework", limit=50) == []
        assert eng.hybrid_search("widgets OR notes OR framework", limit=50) == []


# ---------------------------------------------------------------------------
# Sanity: same DB, populated scope still sees its own data (regression guard
# so a future bug that breaks both scopes equally doesn't pass these tests).
# ---------------------------------------------------------------------------


def test_populated_scope_sees_its_own_data(populated_db: Path) -> None:
    with Engrama(backend="sqlite", db_path=populated_db, **_DATA_SCOPE_KW) as eng:
        names = {h["name"] for h in eng.search("widgets OR notes OR framework", limit=50)}
        scope = eng._engine.default_scope
        profile = eng._store.count_labels(scope=scope)
    assert "rich-proj" in names or "rich-tech" in names
    assert profile.get("Project", 0) >= 1


def test_partial_scope_is_match_nothing(populated_db: Path) -> None:
    """A partial scope (only ``org_id``, no ``user_id``) is illegal —
    the helpers fail closed. The SDK constructor mirrors a lone identity
    into the pair (org == user), so this asserts the *helper* directly
    rather than via the SDK convenience.
    """
    from engrama.backends.sqlite import SqliteGraphStore

    s = SqliteGraphStore(populated_db)
    try:
        partial = MemoryScope(org_id="globex-rich", user_id=None)
        assert s.count_labels(scope=partial) == {}
        assert s.list_existing_nodes(scope=partial) == []
        assert s.detect_concept_clusters(scope=partial) == []
    finally:
        s.close()
