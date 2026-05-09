"""Tests for the SQLite graph store backend.

Runs without any external service — exercises an in-memory SQLite
database so CI doesn't need Neo4j, Docker, or filesystem state.
"""

from __future__ import annotations

import pytest

from engrama.backends.sqlite import SqliteGraphStore


@pytest.fixture()
def store(tmp_path):
    s = SqliteGraphStore(tmp_path / "test.db")
    yield s
    s.close()


# ----------------------------------------------------------------------
# Lifecycle / health
# ----------------------------------------------------------------------


def test_health_check_reports_sqlite(store):
    h = store.health_check()
    assert h["ok"] is True
    assert h["backend"] == "sqlite"
    assert h["node_count"] == 0
    assert h["sqlite_version"]


def test_init_schema_is_idempotent(store):
    # Connection ctor already initialised the schema; this should be safe.
    store.init_schema()
    store.init_schema()
    assert store.health_check()["ok"]


# ----------------------------------------------------------------------
# Node operations
# ----------------------------------------------------------------------


def test_merge_node_creates(store):
    result = store.merge_node(
        "Project", "name", "test-proj",
        {"status": "active", "description": "demo"},
    )
    assert len(result) == 1
    n = result[0]["n"]
    assert n["_labels"] == ["Project"]
    assert n["name"] == "test-proj"
    assert n["status"] == "active"
    assert n["created_at"] == n["updated_at"]
    assert n["confidence"] == 1.0  # default
    assert n["valid_from"]


def test_merge_node_updates_preserves_created_at(store):
    a = store.merge_node("Project", "name", "p1", {"status": "active"})[0]["n"]
    b = store.merge_node("Project", "name", "p1", {"status": "paused"})[0]["n"]
    assert a["_id"] == b["_id"]
    assert b["created_at"] == a["created_at"]
    assert b["updated_at"] >= a["updated_at"]
    assert b["status"] == "paused"


def test_merge_node_merges_props_does_not_drop(store):
    store.merge_node("Project", "name", "p1", {"status": "active", "stack": ["python"]})
    n = store.merge_node("Project", "name", "p1", {"description": "added"})[0]["n"]
    assert n["status"] == "active"          # preserved
    assert n["stack"] == ["python"]         # preserved
    assert n["description"] == "added"      # added


def test_get_node(store):
    store.merge_node("Concept", "name", "graphs", {"domain": "cs"})
    n = store.get_node("Concept", "name", "graphs")
    assert n["domain"] == "cs"
    assert n["name"] == "graphs"
    assert n["created_at"]
    assert store.get_node("Concept", "name", "missing") is None


def test_delete_node_soft(store):
    store.merge_node("Project", "name", "p1", {})
    assert store.delete_node("Project", "name", "p1", soft=True) is True
    n = store.get_node("Project", "name", "p1")
    assert n["status"] == "archived"
    assert n["archived_at"]


def test_delete_node_hard(store):
    store.merge_node("Project", "name", "p1", {})
    assert store.delete_node("Project", "name", "p1", soft=False) is True
    assert store.get_node("Project", "name", "p1") is None


def test_archive_node_by_name(store):
    store.merge_node("Project", "name", "to-forget", {"status": "active"})
    out = store.archive_node_by_name("Project", "to-forget")
    assert out["archived"] is True
    assert out["node"]["name"] == "to-forget"
    assert out["node"]["archived_at"]
    assert store.archive_node_by_name("Project", "missing")["archived"] is False


def test_list_existing_nodes(store):
    store.merge_node("Project", "name", "alpha", {})
    store.merge_node("Concept", "name", "beta", {})
    out = store.list_existing_nodes()
    assert {"label": "Project", "name": "alpha"} in out
    assert {"label": "Concept", "name": "beta"} in out


# ----------------------------------------------------------------------
# Relationship operations
# ----------------------------------------------------------------------


def test_merge_relation_creates(store):
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Technology", "name", "python", {})
    out = store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "python")
    assert out and out[0]["rel_type"] == "USES"


def test_merge_relation_idempotent(store):
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Technology", "name", "python", {})
    store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "python")
    store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "python")
    # Only one neighbour should appear.
    rows = store.get_neighbours("Project", "name", "p", hops=1)
    assert len(rows) == 1


def test_merge_relation_silent_when_endpoint_missing(store):
    store.merge_node("Project", "name", "p", {})
    out = store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "missing")
    assert out == []


# ----------------------------------------------------------------------
# Traversal
# ----------------------------------------------------------------------


def test_get_neighbours_one_hop(store):
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Technology", "name", "python", {})
    store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "python")
    rows = store.get_neighbours("Project", "name", "p", hops=1)
    assert len(rows) == 1
    row = rows[0]
    assert row["start"]["_labels"] == ["Project"]
    assert row["neighbour"]["_labels"] == ["Technology"]
    assert row["neighbour"]["name"] == "python"
    assert row["rel"][0]["_type"] == "USES"


def test_get_neighbours_two_hops_reaches_further(store):
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Technology", "name", "python", {})
    store.merge_node("Concept", "name", "graphs", {})
    store.merge_relation("Project",   "name", "p",      "USES",     "Technology", "name", "python")
    store.merge_relation("Technology", "name", "python", "APPLIES", "Concept",    "name", "graphs")
    one_hop = store.get_neighbours("Project", "name", "p", hops=1)
    two_hop = store.get_neighbours("Project", "name", "p", hops=2)
    one_hop_names = {r["neighbour"]["name"] for r in one_hop}
    two_hop_names = {r["neighbour"]["name"] for r in two_hop}
    assert one_hop_names == {"python"}
    assert two_hop_names == {"python", "graphs"}


def test_get_neighbours_traverses_undirected(store):
    """Edges walked in both directions, mirroring Neo4j ``-[r*1..N]-``."""
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Person",  "name", "alice", {})
    # Edge points alice -> p; querying from p must still find alice.
    store.merge_relation("Person", "name", "alice", "BELONGS_TO", "Project", "name", "p")
    rows = store.get_neighbours("Project", "name", "p", hops=1)
    assert {r["neighbour"]["name"] for r in rows} == {"alice"}


def test_get_node_with_neighbours(store):
    store.merge_node("Project", "name", "p", {"description": "the proj"})
    store.merge_node("Technology", "name", "python", {})
    store.merge_relation("Project", "name", "p", "USES", "Technology", "name", "python")
    out = store.get_node_with_neighbours("Project", "name", "p", hops=1)
    assert out["node"]["name"] == "p"
    assert out["node"]["description"] == "the proj"
    assert out["neighbours"][0]["name"] == "python"
    assert out["neighbours"][0]["via"] == ["USES"]


def test_get_node_with_neighbours_returns_none_for_missing(store):
    assert store.get_node_with_neighbours("Project", "name", "ghost") is None


# ----------------------------------------------------------------------
# Lookup helpers
# ----------------------------------------------------------------------


def test_lookup_node_label_finds_by_name_or_title(store):
    store.merge_node("Project",  "name",  "alpha", {})
    store.merge_node("Decision", "title", "go-rest", {})
    assert store.lookup_node_label("alpha") == "Project"
    assert store.lookup_node_label("go-rest") == "Decision"
    assert store.lookup_node_label("missing") is None


def test_lookup_node_label_is_case_insensitive(store):
    store.merge_node("Project", "name", "MixedCase", {})
    assert store.lookup_node_label("mixedcase") == "Project"


def test_count_labels_excludes_insights(store):
    store.merge_node("Project", "name", "p", {})
    store.merge_node("Project", "name", "q", {})
    store.merge_node("Insight", "title", "i1", {})
    counts = store.count_labels()
    assert counts.get("Project") == 2
    assert "Insight" not in counts


# ----------------------------------------------------------------------
# Fulltext
# ----------------------------------------------------------------------


def test_fulltext_search_matches_description(store):
    store.merge_node("Project", "name", "alpha",
                     {"description": "graph database memory engine"})
    out = store.fulltext_search("memory")
    assert any(r["name"] == "alpha" for r in out)


def test_fulltext_search_returns_summary_or_description(store):
    store.merge_node("Project", "name", "alpha",
                     {"description": "the description", "summary": "the summary"})
    store.merge_node("Project", "name", "beta",
                     {"description": "only a description here"})
    out = store.fulltext_search("description")
    by_name = {r["name"]: r for r in out}
    assert by_name["alpha"]["summary"] == "the summary"
    assert by_name["beta"]["summary"] == "only a description here"


def test_fulltext_search_empty_query_returns_empty(store):
    store.merge_node("Project", "name", "p", {"description": "x"})
    assert store.fulltext_search("") == []
    assert store.fulltext_search("   ") == []


def test_fulltext_search_invalid_syntax_returns_empty(store):
    """A malformed FTS5 query must not raise — caller-friendly degradation."""
    store.merge_node("Project", "name", "p", {"description": "x"})
    assert store.fulltext_search('"unbalanced') == []


def test_fulltext_indexes_tags_as_text(store):
    store.merge_node("Project", "name", "alpha", {"tags": ["security", "graphdb"]})
    out = store.fulltext_search("security")
    assert any(r["name"] == "alpha" for r in out)


# ----------------------------------------------------------------------
# Cypher escape hatch
# ----------------------------------------------------------------------


def test_run_cypher_is_not_supported(store):
    with pytest.raises(NotImplementedError):
        store.run_cypher("MATCH (n) RETURN n")


# ----------------------------------------------------------------------
# Insights
# ----------------------------------------------------------------------


def test_insight_lifecycle_pending_to_approved(store):
    store.merge_node("Insight", "title", "i1", {
        "body": "an insight",
        "confidence": 0.9,
        "status": "pending",
        "source_query": "q1",
    })
    pending = store.get_pending_insights()
    assert any(p["title"] == "i1" and p["confidence"] == 0.9 for p in pending)
    assert store.update_insight_status("i1", "approved") is True
    assert store.get_pending_insights() == []
    by_title = store.get_insight_by_title("i1")
    assert by_title["status"] == "approved"


def test_get_pending_insights_orders_by_confidence(store):
    store.merge_node("Insight", "title", "low",  {"confidence": 0.3, "status": "pending"})
    store.merge_node("Insight", "title", "high", {"confidence": 0.9, "status": "pending"})
    store.merge_node("Insight", "title", "mid",  {"confidence": 0.6, "status": "pending"})
    titles = [p["title"] for p in store.get_pending_insights()]
    assert titles == ["high", "mid", "low"]


def test_dismissed_insights_excluded_from_pending(store):
    store.merge_node("Insight", "title", "kept", {"confidence": 0.5, "status": "pending"})
    store.merge_node("Insight", "title", "drop", {"confidence": 0.5, "status": "pending"})
    store.update_insight_status("drop", "dismissed")
    titles = {p["title"] for p in store.get_pending_insights()}
    assert "kept" in titles and "drop" not in titles
    assert store.get_dismissed_insight_titles() == {"drop"}


def test_mark_insight_synced(store):
    store.merge_node("Insight", "title", "i1", {"confidence": 0.5, "status": "approved"})
    assert store.mark_insight_synced("i1", "vault/insights/i1.md") is True
    by_title = store.get_insight_by_title("i1")
    # mark_insight_synced sets obsidian_path in props; not in get_insight_by_title
    # selection but visible via get_node.
    full = store.get_node("Insight", "title", "i1")
    assert full["obsidian_path"] == "vault/insights/i1.md"
    assert full["synced_at"]
    assert by_title["status"] == "approved"


def test_find_insight_by_source_query(store):
    store.merge_node("Insight", "title", "i1", {
        "confidence": 0.5, "status": "pending", "source_query": "qX",
    })
    found = store.find_insight_by_source_query("qX")
    assert found["title"] == "i1"
    assert store.find_insight_by_source_query("nope") is None


# ----------------------------------------------------------------------
# Temporal
# ----------------------------------------------------------------------


def test_expire_node_sets_valid_to(store):
    store.merge_node("Project", "name", "p", {})
    assert store.expire_node("Project", "name", "p") is True
    n = store.get_node("Project", "name", "p")
    assert n["valid_to"]


def test_expire_then_remerge_revives(store):
    store.merge_node("Project", "name", "p", {})
    store.expire_node("Project", "name", "p")
    n = store.merge_node("Project", "name", "p", {"description": "back"})[0]["n"]
    assert "valid_to" not in n


def test_decay_scores_reduces_old_confidence(store):
    """Decay applies when updated_at is in the past."""
    import datetime as dt
    import json as _json
    # Backdate a node manually so decay has something to chew on.
    store.merge_node("Project", "name", "p", {"description": "x"})
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=30)).isoformat()
    store._conn.execute(
        "UPDATE nodes SET updated_at = ?, "
        "props = json_set(props, '$.confidence', 1.0) WHERE label = 'Project'",
        (old,),
    )
    store._conn.commit()
    out = store.decay_scores(rate=0.05)
    assert out["decayed"] >= 1
    n = store.get_node("Project", "name", "p")
    assert n["confidence"] < 1.0


def test_decay_scores_archives_below_threshold(store):
    import datetime as dt
    store.merge_node("Project", "name", "p", {})
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=365 * 5)).isoformat()
    store._conn.execute(
        "UPDATE nodes SET updated_at = ?, "
        "props = json_set(props, '$.confidence', 0.5) WHERE label = 'Project'",
        (old,),
    )
    store._conn.commit()
    out = store.decay_scores(rate=0.5, min_confidence=0.1)
    assert out["archived"] >= 1
    n = store.get_node("Project", "name", "p")
    assert n["status"] == "archived"


def test_query_at_date_filters_validity_window(store):
    store.merge_node("Project", "name", "old", {
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to":   "2026-02-01T00:00:00+00:00",
    })
    store.merge_node("Project", "name", "current", {
        "valid_from": "2026-01-01T00:00:00+00:00",
    })
    on_jan = {r["name"] for r in store.query_at_date("2026-01-15T00:00:00+00:00")}
    on_mar = {r["name"] for r in store.query_at_date("2026-03-15T00:00:00+00:00")}
    assert "old" in on_jan and "current" in on_jan
    assert "current" in on_mar and "old" not in on_mar


def test_archive_nodes_older_than(store):
    import datetime as dt
    store.merge_node("Project", "name", "stale", {})
    store.merge_node("Project", "name", "fresh", {})
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=400)).isoformat()
    store._conn.execute(
        "UPDATE nodes SET updated_at = ? WHERE key_value = 'stale'", (old,),
    )
    store._conn.commit()
    out = store.archive_nodes_older_than("Project", days=180)
    assert out["affected"] == 1
    assert store.get_node("Project", "name", "stale")["status"] == "archived"
    assert store.get_node("Project", "name", "fresh").get("status") != "archived"


# ----------------------------------------------------------------------
# Obsidian helpers
# ----------------------------------------------------------------------


def test_find_obsidian_path(store):
    store.merge_node("Project", "name", "p", {"obsidian_path": "vault/p.md"})
    assert store.find_obsidian_path("Project", "p") == "vault/p.md"
    store.merge_node("Project", "name", "q", {})
    assert store.find_obsidian_path("Project", "q") is None
    assert store.find_obsidian_path("Project", "missing") is None


def test_list_documented_nodes_filters_by_obsidian_path(store):
    store.merge_node("Project", "name", "withpath", {"obsidian_path": "vault/x.md"})
    store.merge_node("Project", "name", "nopath", {})
    out = store.list_documented_nodes()
    names = {r["name"] for r in out}
    assert names == {"withpath"}


def test_archive_node_for_missing_note(store):
    store.merge_node("Project", "name", "to-archive", {"obsidian_path": "vault/x.md"})
    assert store.archive_node_for_missing_note("Project", "to-archive") is True
    assert store.get_node("Project", "name", "to-archive")["status"] == "archived"
    assert store.archive_node_for_missing_note("Project", "missing") is False


def test_merge_wiki_link_creates_links_to(store):
    store.merge_node("Project", "name", "a", {})
    store.merge_node("Concept", "name", "b", {})
    store.merge_wiki_link(from_label="Project", from_name="a", to_label="Concept", to_name="b")
    rows = store.get_neighbours("Project", "name", "a", hops=1)
    assert any(r["neighbour"]["name"] == "b" and r["rel"][0]["_type"] == "LINKS_TO" for r in rows)


def test_merge_wiki_link_by_target_name_resolves_label(store):
    store.merge_node("Project", "name", "a", {})
    store.merge_node("Concept", "name", "B", {})
    n = store.merge_wiki_link_by_target_name(
        from_label="Project", from_name="a", target_name="b",
    )
    assert n == 1
    rows = store.get_neighbours("Project", "name", "a", hops=1)
    assert any(r["neighbour"]["name"] == "B" for r in rows)


# ----------------------------------------------------------------------
# Seed / CLI helpers
# ----------------------------------------------------------------------


def test_seed_domain_and_concept(store):
    store.seed_domain("cybersecurity", "security domain")
    store.seed_concept_in_domain("threat-modelling", "cybersecurity")
    rows = store.get_neighbours("Concept", "name", "threat-modelling", hops=1)
    assert any(
        r["neighbour"]["name"] == "cybersecurity" and r["rel"][0]["_type"] == "IN_DOMAIN"
        for r in rows
    )


def test_apply_schema_statements_returns_failures(store):
    out = store.apply_schema_statements(["CREATE INDEX neo_only FOR (n:Project)"])
    assert len(out) == 1
    assert isinstance(out[0][1], NotImplementedError)


def test_list_nodes_for_embedding_force(store):
    store.merge_node("Project", "name", "a", {})
    store.merge_node("Project", "name", "b", {})
    out = store.list_nodes_for_embedding(force=True)
    names = {n["props"].get("name") for n in out}
    assert {"a", "b"}.issubset(names)


# ----------------------------------------------------------------------
# Reflect — pattern detection
# ----------------------------------------------------------------------


def _build_cross_project_scenario(store):
    """Project A solved a Problem with a Decision; Project B has an open
    Problem involving the same Concept. Expected: pattern matches.
    """
    # Source side
    store.merge_node("Project",  "name",  "alpha", {})
    store.merge_node("Problem",  "title", "leak",  {"status": "resolved"})
    store.merge_node("Decision", "title", "use-tls", {})
    store.merge_node("Concept",  "name",  "encryption", {})
    store.merge_relation("Project", "name", "alpha", "HAS",          "Problem", "title", "leak")
    store.merge_relation("Problem", "title", "leak", "INSTANCE_OF", "Concept", "name", "encryption")
    store.merge_relation("Problem", "title", "leak", "SOLVED_BY",   "Decision", "title", "use-tls")
    store.merge_relation("Project", "name", "alpha", "INFORMED_BY", "Decision", "title", "use-tls")
    # Target side — open problem on a different project
    store.merge_node("Project", "name",  "beta", {})
    store.merge_node("Problem", "title", "exposed-creds", {"status": "open"})
    store.merge_relation("Project", "name", "beta", "HAS",          "Problem", "title", "exposed-creds")
    store.merge_relation("Problem", "title", "exposed-creds", "APPLIES", "Concept", "name", "encryption")


def test_detect_cross_project_solutions_finds_match(store):
    _build_cross_project_scenario(store)
    rows = store.detect_cross_project_solutions()
    assert any(
        r["target_project"] == "beta"
        and r["source_project"] == "alpha"
        and r["decision"] == "use-tls"
        and r["concept"] == "encryption"
        and r["open_problem"] == "exposed-creds"
        for r in rows
    )


def test_detect_cross_project_solutions_excludes_self(store):
    """Same project on both sides must NOT match (pA != pB)."""
    store.merge_node("Project",  "name",  "alpha", {})
    store.merge_node("Problem",  "title", "leak",   {"status": "resolved"})
    store.merge_node("Problem",  "title", "open",   {"status": "open"})
    store.merge_node("Decision", "title", "fix",    {})
    store.merge_node("Concept",  "name",  "auth",   {})
    store.merge_relation("Project", "name", "alpha", "HAS",         "Problem", "title", "open")
    store.merge_relation("Problem", "title", "open",  "INSTANCE_OF", "Concept", "name", "auth")
    store.merge_relation("Problem", "title", "leak",  "INSTANCE_OF", "Concept", "name", "auth")
    store.merge_relation("Problem", "title", "leak",  "SOLVED_BY",   "Decision", "title", "fix")
    store.merge_relation("Project", "name", "alpha", "INFORMED_BY", "Decision", "title", "fix")
    rows = store.detect_cross_project_solutions()
    assert rows == []


def test_detect_shared_technology(store):
    store.merge_node("Project",    "name", "alpha", {})
    store.merge_node("Project",    "name", "beta",  {})
    store.merge_node("Technology", "name", "python", {})
    store.merge_relation("Project", "name", "alpha", "USES", "Technology", "name", "python")
    store.merge_relation("Project", "name", "beta",  "USES", "Technology", "name", "python")
    rows = store.detect_shared_technology()
    assert any(
        {r["entity_a"], r["entity_b"]} == {"alpha", "beta"} and r["technology"] == "python"
        for r in rows
    )


def test_detect_shared_technology_no_self_pair(store):
    store.merge_node("Project",    "name", "alpha",  {})
    store.merge_node("Technology", "name", "python", {})
    store.merge_relation("Project", "name", "alpha", "USES", "Technology", "name", "python")
    assert store.detect_shared_technology() == []


def test_detect_training_opportunities_picks_vuln_and_open_problem(store):
    store.merge_node("Vulnerability", "title", "sqli", {})
    store.merge_node("Problem",       "title", "auth-bypass", {"status": "open"})
    store.merge_node("Problem",       "title", "old-issue",   {"status": "resolved"})
    store.merge_node("Concept",       "name",  "input-validation", {})
    store.merge_node("Course",        "name",  "secure-coding", {})
    store.merge_relation("Vulnerability", "title", "sqli",        "APPLIES",     "Concept", "name", "input-validation")
    store.merge_relation("Problem",       "title", "auth-bypass", "APPLIES",     "Concept", "name", "input-validation")
    store.merge_relation("Problem",       "title", "old-issue",   "APPLIES",     "Concept", "name", "input-validation")
    store.merge_relation("Course",        "name",  "secure-coding", "COVERS",    "Concept", "name", "input-validation")
    out = store.detect_training_opportunities()
    issues = {r["issue"] for r in out}
    assert "sqli" in issues and "auth-bypass" in issues
    assert "old-issue" not in issues   # resolved problems excluded


def test_detect_technique_transfer(store):
    """Technique IN_DOMAIN appsec applies to a Concept also covered by
    something IN_DOMAIN ml — suggests transfer.
    """
    store.merge_node("Domain",    "name", "appsec", {})
    store.merge_node("Domain",    "name", "ml",     {})
    store.merge_node("Technique", "name", "fuzzing", {})
    store.merge_node("Concept",   "name", "input-perturbation", {})
    store.merge_node("Tool",      "name", "ml-fuzzer", {})
    store.merge_relation("Technique", "name", "fuzzing",     "IN_DOMAIN",   "Domain",  "name", "appsec")
    store.merge_relation("Tool",      "name", "ml-fuzzer",   "IN_DOMAIN",   "Domain",  "name", "ml")
    store.merge_relation("Technique", "name", "fuzzing",     "APPLIES",     "Concept", "name", "input-perturbation")
    store.merge_relation("Tool",      "name", "ml-fuzzer",   "APPLIES",     "Concept", "name", "input-perturbation")
    out = store.detect_technique_transfer()
    assert any(
        r["technique"] == "fuzzing"
        and r["source_domain"] == "appsec"
        and r["target_domain"] == "ml"
        and r["related_entities"] >= 1
        for r in out
    )


def test_detect_concept_clusters_finds_three_or_more(store):
    store.merge_node("Concept", "name", "graphs", {})
    for proj in ["a", "b", "c", "d"]:
        store.merge_node("Project", "name", proj, {})
        store.merge_relation("Project", "name", proj, "APPLIES", "Concept", "name", "graphs")
    out = store.detect_concept_clusters()
    assert any(r["concept"] == "graphs" and r["entity_count"] == 4 for r in out)
    sample = [r for r in out if r["concept"] == "graphs"][0]["sample"]
    assert len(sample) <= 5
    assert all("name" in s and "label" in s for s in sample)


def test_detect_concept_clusters_skips_low_count(store):
    store.merge_node("Concept", "name", "rare", {})
    store.merge_node("Project", "name", "p", {})
    store.merge_relation("Project", "name", "p", "APPLIES", "Concept", "name", "rare")
    out = store.detect_concept_clusters()
    assert all(r["concept"] != "rare" for r in out)


def test_detect_stale_knowledge_picks_old_or_low_conf(store):
    import datetime as dt
    store.merge_node("Project",    "name",  "alpha", {"status": "active"})
    store.merge_node("Technology", "name",  "rusty", {"confidence": 1.0})
    store.merge_node("Technology", "name",  "wobbly", {"confidence": 0.1})  # low confidence
    store.merge_relation("Project", "name", "alpha", "USES", "Technology", "name", "rusty")
    store.merge_relation("Project", "name", "alpha", "USES", "Technology", "name", "wobbly")
    # Age the rusty node
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=180)).isoformat()
    store._conn.execute(
        "UPDATE nodes SET updated_at = ? WHERE key_value = 'rusty'", (old,),
    )
    store._conn.commit()
    out = store.detect_stale_knowledge()
    names = {r["name"] for r in out}
    assert {"rusty", "wobbly"}.issubset(names)


def test_detect_under_connected_nodes(store):
    store.merge_node("Project",    "name", "lonely", {})  # 0 edges
    store.merge_node("Project",    "name", "popular", {})
    store.merge_node("Technology", "name", "x", {})
    store.merge_node("Technology", "name", "y", {})
    store.merge_relation("Project", "name", "popular", "USES", "Technology", "name", "x")
    store.merge_relation("Project", "name", "popular", "USES", "Technology", "name", "y")
    out = store.detect_under_connected_nodes()
    by_name = {r["name"]: r for r in out}
    assert "lonely" in by_name and by_name["lonely"]["rel_count"] == 0
    # popular has 2 edges → excluded by HAVING < 2
    assert "popular" not in by_name


def test_detect_under_connected_skips_archived(store):
    store.merge_node("Project", "name", "ghost", {})
    store.delete_node("Project", "name", "ghost", soft=True)
    out = store.detect_under_connected_nodes()
    assert all(r["name"] != "ghost" for r in out)
