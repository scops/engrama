"""Cypher for the reflect detectors, shared by the sync and async Neo4j stores.

Each function returns ``(cypher, params)`` for one detector, with every matched
node restricted to the caller's scope (Spec 001 FR-12; a ``None``/incomplete
scope yields ``(false)`` per alias and zero rows) and to *live* nodes: not
archived, not superseded, and not a reflect-generated Insight (DDR-008).
Hand-written Insights are ordinary domain nodes here (DDR-006).

Keeping the queries in one place is what keeps the two stores' reflect
results identical; the SQLite store mirrors them in SQL and a parity test
checks all three.
"""

from __future__ import annotations

from typing import Any

from engrama.core.reflection import SHARED_TECHNOLOGY_MIN_MEMBERS
from engrama.core.scope import MemoryScope, scope_filter_cypher
from engrama.core.stubs import HUB_STUB_MIN_DEGREE


def live(var: str) -> str:
    """Null-safe predicate: ``var`` is live (DDR-008 item 1)."""
    return (
        f"NOT coalesce({var}.status, 'active') IN ['archived', 'superseded'] "
        f"AND NOT ({var}:Insight AND {var}.source_query IS NOT NULL)"
    )


def label(var: str) -> str:
    """The node's primary label, skipping the secondary ``:Embedded`` label."""
    return f"[l IN labels({var}) WHERE l <> 'Embedded'][0]"


def _scope_and(node_vars: tuple[str, ...], scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for var in node_vars:
        clause, p = scope_filter_cypher(scope, var)
        clauses.append(clause)
        params.update(p)
    return " AND ".join(clauses), params


def _live_and(node_vars: tuple[str, ...]) -> str:
    return " AND ".join(f"({live(v)})" for v in node_vars)


def cross_project_solutions(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """Open Problem shares a Concept with a resolved Problem in another
    Project that has a Decision."""
    scope_sql, params = _scope_and(("pB", "open", "c", "resolved", "d", "pA"), scope)
    cypher = (
        "MATCH (pB:Project)-[:HAS]->(open:Problem {status: $open_status}) "
        "MATCH (open)-[:INSTANCE_OF|APPLIES]->(c:Concept)"
        "<-[:INSTANCE_OF|APPLIES]-(resolved:Problem {status: $resolved_status}) "
        "MATCH (resolved)-[:SOLVED_BY]->(d:Decision)<-[:INFORMED_BY]-(pA:Project) "
        f"WHERE pA <> pB AND {scope_sql} AND {_live_and(('pB', 'c', 'd', 'pA'))} "
        "RETURN pB.name AS target_project, open.title AS open_problem, "
        "d.title AS decision, pA.name AS source_project, c.name AS concept"
    )
    return cypher, {"open_status": "open", "resolved_status": "resolved", **params}


def shared_technology(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """One row per Technology with enough live users, listing them."""
    scope_sql, params = _scope_and(("m", "t"), scope)
    cypher = (
        "MATCH (m)-[:USES|TEACHES|COMPOSED_OF]->(t:Technology) "
        f"WHERE {scope_sql} AND {_live_and(('m', 't'))} "
        "WITH t, collect(DISTINCT {name: coalesce(m.name, m.title), "
        f"label: {label('m')}}}) AS members "
        "WHERE size(members) >= $min_members "
        "RETURN t.name AS technology, members "
        "ORDER BY size(members) DESC, technology"
    )
    return cypher, {"min_members": SHARED_TECHNOLOGY_MIN_MEMBERS, **params}


def training_opportunities(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """A Vulnerability or open Problem shares a Concept with a Course."""
    scope_sql, params = _scope_and(("issue", "c", "course"), scope)
    cypher = (
        "MATCH (issue)-[:INSTANCE_OF|APPLIES]->(c:Concept)<-[:COVERS]-(course:Course) "
        "WHERE ((issue:Vulnerability) OR (issue:Problem AND issue.status = $open_status)) "
        f"AND {scope_sql} AND {_live_and(('issue', 'c', 'course'))} "
        "RETURN coalesce(issue.title, issue.name) AS issue, "
        f"{label('issue')} AS issue_type, c.name AS concept, course.name AS course"
    )
    return cypher, {"open_status": "open", **params}


def technique_transfer(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """Technique used in domain A could apply in domain B."""
    scope_sql, params = _scope_and(("t", "d1", "d2", "other"), scope)
    cypher = (
        "MATCH (t:Technique)-[:IN_DOMAIN]->(d1:Domain) "
        "MATCH (d2:Domain) WHERE d1 <> d2 "
        "AND NOT EXISTS { MATCH (t)-[:IN_DOMAIN]->(d2) } "
        "MATCH (other)-[:IN_DOMAIN]->(d2) "
        "WHERE (other)-[:INSTANCE_OF|APPLIES]->(:Concept)<-[:INSTANCE_OF|APPLIES]-(t) "
        f"AND {scope_sql} AND {_live_and(('t', 'd1', 'd2', 'other'))} "
        "RETURN t.name AS technique, d1.name AS source_domain, "
        "d2.name AS target_domain, count(other) AS related_entities "
        "ORDER BY related_entities DESC, technique, target_domain LIMIT 10"
    )
    return cypher, params


def concept_clusters(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """Concept connected to >= 3 live entities."""
    scope_sql, params = _scope_and(("c", "n"), scope)
    cypher = (
        "MATCH (c:Concept)<-[:INSTANCE_OF|APPLIES]-(n) "
        f"WHERE {scope_sql} AND {_live_and(('c', 'n'))} "
        "WITH c, collect(DISTINCT {name: coalesce(n.name, n.title), "
        f"label: {label('n')}}}) AS connected "
        "WHERE size(connected) >= 3 "
        "RETURN c.name AS concept, size(connected) AS entity_count, "
        "connected[..5] AS sample "
        "ORDER BY entity_count DESC, concept LIMIT 10"
    )
    return cypher, params


def stale_knowledge(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """Live nodes 90d+ stale or low-confidence, linked to an active Project/Course."""
    scope_sql, params = _scope_and(("n", "active"), scope)
    cypher = (
        "MATCH (n)-[r]-(active) "
        "WHERE (active:Project OR active:Course) "
        "AND (active.status IS NULL OR active.status IN [$active_status, 'active']) "
        "AND ("
        "  n.updated_at < datetime() - duration({days: 90}) "
        "  OR (n.confidence IS NOT NULL AND n.confidence < 0.3)"
        ") "
        f"AND NOT n:Project AND NOT n:Course AND NOT n:Domain "
        f"AND {scope_sql} AND {_live_and(('n',))} "
        f"RETURN coalesce(n.name, n.title) AS name, {label('n')} AS label, "
        "n.updated_at AS last_updated, n.confidence AS confidence, "
        "active.name AS project, type(r) AS rel "
        "ORDER BY coalesce(n.confidence, 1.0) ASC, n.updated_at ASC, name LIMIT 15"
    )
    return cypher, {"active_status": "active", **params}


def under_connected_nodes(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """Every live node with fewer than 2 substantive relationships.

    Edges to stubs and to reflect-generated Insights don't count. No limit:
    the builder reports the total and samples the most isolated nodes.
    """
    scope_sql, params = _scope_and(("n",), scope)
    cypher = (
        "MATCH (n) WHERE NOT n:Domain "
        "AND coalesce(n.name, n.title) IS NOT NULL "
        f"AND {scope_sql} AND {_live_and(('n',))} "
        "WITH n, size([(n)-[]-(m) "
        "WHERE coalesce(m.status, 'active') <> 'stub' "
        "AND NOT (m:Insight AND m.source_query IS NOT NULL) | 1]) AS rel_count "
        "WHERE rel_count < 2 "
        f"RETURN coalesce(n.name, n.title) AS name, {label('n')} AS label, "
        "rel_count, n.created_at AS created "
        "ORDER BY rel_count ASC, n.created_at DESC, name"
    )
    return cypher, params


def hub_stubs(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """Stubs holding at least ``HUB_STUB_MIN_DEGREE`` substantive edges."""
    scope_sql, params = _scope_and(("n",), scope)
    cypher = (
        "MATCH (n) WHERE n.status = 'stub' AND coalesce(n.name, n.title) IS NOT NULL "
        f"AND {scope_sql} "
        "WITH n, size([(n)-[]-(m) "
        "WHERE NOT (m:Insight AND m.source_query IS NOT NULL) | 1]) AS degree "
        "WHERE degree >= $min_degree "
        f"RETURN coalesce(n.name, n.title) AS name, {label('n')} AS label, degree "
        "ORDER BY degree DESC, name"
    )
    return cypher, {"min_degree": HUB_STUB_MIN_DEGREE, **params}


def name_candidates(
    name: str, fragments: list[str], scope: MemoryScope | None, limit: int
) -> tuple[str, dict[str, Any]]:
    """In-scope nodes whose lowercased name contains one of ``fragments``,
    exact names and similar lengths first (entity resolution, DDR-006)."""
    scope_sql, params = _scope_and(("n",), scope)
    cypher = (
        "MATCH (n) WHERE coalesce(n.name, n.title) IS NOT NULL "
        f"AND {scope_sql} AND NOT (n:Insight AND n.source_query IS NOT NULL) "
        "WITH n, coalesce(n.name, n.title) AS key "
        "WHERE any(f IN $fragments WHERE toLower(key) CONTAINS f) "
        f"RETURN {label('n')} AS label, key AS name, n.status AS status "
        "ORDER BY toLower(key) = toLower($name) DESC, "
        "abs(size(key) - size($name)), key LIMIT $limit"
    )
    return cypher, {"name": name, "fragments": fragments, "limit": limit, **params}


def anchors(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """Live anchor nodes (Project/Client/Course/Domain) as ``{label, name}``."""
    scope_sql, params = _scope_and(("n",), scope)
    cypher = (
        "MATCH (n) WHERE (n:Project OR n:Client OR n:Course OR n:Domain) "
        f"AND coalesce(n.name, n.title) IS NOT NULL AND {scope_sql} AND {_live_and(('n',))} "
        f"RETURN {label('n')} AS label, coalesce(n.name, n.title) AS name"
    )
    return cypher, params


def health_nodes(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """Every node in scope, projected onto the fields ``core.health`` needs."""
    scope_sql, params = _scope_and(("n",), scope)
    cypher = (
        f"MATCH (n) WHERE {scope_sql} AND coalesce(n.name, n.title) IS NOT NULL "
        f"RETURN elementId(n) AS id, {label('n')} AS label, "
        "       coalesce(n.name, n.title) AS key, n.status AS status, "
        "       n.tags AS tags, n.confidence AS confidence, "
        "       n.source_query AS source_query, "
        "       coalesce(n.summary, '') <> '' AS has_summary, "
        "       n.engrama_id IS NOT NULL AS has_engrama_id, "
        "       n.source IS NOT NULL AS has_source, "
        "       n.trust_level IS NOT NULL AS has_trust"
    )
    return cypher, params


def health_edges(scope: MemoryScope | None) -> tuple[str, dict[str, Any]]:
    """Every edge whose two endpoints are visible in scope."""
    scope_sql, params = _scope_and(("a", "b"), scope)
    cypher = f"MATCH (a)-[]->(b) WHERE {scope_sql} RETURN elementId(a) AS a, elementId(b) AS b"
    return cypher, params


__all__ = [
    "anchors",
    "name_candidates",
    "concept_clusters",
    "health_edges",
    "health_nodes",
    "hub_stubs",
    "cross_project_solutions",
    "live",
    "shared_technology",
    "stale_knowledge",
    "technique_transfer",
    "training_opportunities",
    "under_connected_nodes",
]
