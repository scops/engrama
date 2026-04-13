"""
engrama/skills/reflect.py

Adaptive cross-entity pattern detection for the memory graph.

Instead of running a fixed set of hardcoded queries, the reflect skill:

1. **Inspects** the graph to see what labels and relationship types actually
   have data.
2. **Selects** applicable detection queries based on what's present.
3. **Filters** out previously dismissed Insights so the user isn't re-bothered.
4. **Scores** each Insight with a confidence value based on path length,
   supporting connections, and recency.

Detection patterns:

- **Cross-project solution transfer** — an open Problem shares a Concept with
  a resolved Problem that has a Decision.
- **Shared technology** — two active Projects use the same Technology.
- **Training opportunity** — an open Problem shares a Concept with a Course.
- **Technique transfer** — a Technique used in one Domain could apply in
  another Domain where it hasn't been tried.
- **Concept clustering** — multiple unrelated entities share the same Concept
  but the user hasn't noticed the pattern.
- **Stale knowledge** — nodes not updated in 90+ days that connect to active
  Projects (might be outdated).
- **Under-connected nodes** — nodes with fewer than 2 relationships (likely
  under-classified, candidates for enrichment).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engrama.core.schema import Insight

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine


# ---------------------------------------------------------------------------
# Detection query definitions
# ---------------------------------------------------------------------------

# Original three (improved with INSTANCE_OF alongside APPLIES for robustness)

_QUERY_CROSS_PROJECT_SOLUTION = (
    "MATCH (pB:Project)-[:HAS]->(open:Problem {status: $open_status}) "
    "MATCH (open)-[:INSTANCE_OF|APPLIES]->(c:Concept)"
    "<-[:INSTANCE_OF|APPLIES]-(resolved:Problem {status: $resolved_status}) "
    "MATCH (resolved)-[:SOLVED_BY]->(d:Decision)<-[:INFORMED_BY]-(pA:Project) "
    "WHERE pA <> pB "
    "RETURN pB.name AS target_project, open.title AS open_problem, "
    "d.title AS decision, pA.name AS source_project, c.name AS concept"
)

_QUERY_SHARED_TECHNOLOGY = (
    "MATCH (a)-[:USES|TEACHES|COMPOSED_OF]->(t:Technology)"
    "<-[:USES|TEACHES|COMPOSED_OF]-(b) "
    "WHERE id(a) < id(b) "
    "AND NOT a:Insight AND NOT b:Insight "
    "RETURN coalesce(a.name, a.title) AS entity_a, labels(a)[0] AS type_a, "
    "coalesce(b.name, b.title) AS entity_b, labels(b)[0] AS type_b, "
    "t.name AS technology"
)

_QUERY_TRAINING_OPPORTUNITY = (
    "MATCH (issue)-[:INSTANCE_OF|APPLIES]->(c:Concept)<-[:COVERS]-(course:Course) "
    "WHERE (issue:Vulnerability) OR (issue:Problem AND issue.status = $open_status) "
    "RETURN coalesce(issue.title, issue.name) AS issue, "
    "labels(issue)[0] AS issue_type, c.name AS concept, course.name AS course"
)

# New patterns (Phase 2)

_QUERY_TECHNIQUE_TRANSFER = (
    "MATCH (t:Technique)-[:IN_DOMAIN]->(d1:Domain) "
    "MATCH (d2:Domain) WHERE d1 <> d2 "
    "AND NOT EXISTS { MATCH (t)-[:IN_DOMAIN]->(d2) } "
    "MATCH (other)-[:IN_DOMAIN]->(d2) "
    "WHERE (other)-[:INSTANCE_OF|APPLIES]->(:Concept)<-[:INSTANCE_OF|APPLIES]-(t) "
    "RETURN t.name AS technique, d1.name AS source_domain, "
    "d2.name AS target_domain, count(other) AS related_entities "
    "ORDER BY related_entities DESC LIMIT 10"
)

_QUERY_CONCEPT_CLUSTERING = (
    "MATCH (c:Concept)<-[:INSTANCE_OF|APPLIES]-(n) "
    "WITH c, collect(DISTINCT {name: coalesce(n.name, n.title), "
    "label: labels(n)[0]}) AS connected, count(n) AS cnt "
    "WHERE cnt >= 3 "
    "RETURN c.name AS concept, cnt AS entity_count, connected[..5] AS sample "
    "ORDER BY cnt DESC LIMIT 10"
)

_QUERY_STALE_KNOWLEDGE = (
    "MATCH (n)-[r]-(active) "
    "WHERE (active:Project OR active:Course) "
    "AND (active.status IS NULL OR active.status IN [$active_status, 'active']) "
    "AND ("
    "  n.updated_at < datetime() - duration({days: 90}) "
    "  OR (n.confidence IS NOT NULL AND n.confidence < 0.3)"
    ") "
    "AND NOT n:Project AND NOT n:Course AND NOT n:Domain "
    "RETURN coalesce(n.name, n.title) AS name, labels(n)[0] AS label, "
    "n.updated_at AS last_updated, n.confidence AS confidence, "
    "active.name AS project, type(r) AS rel "
    "ORDER BY coalesce(n.confidence, 1.0) ASC, n.updated_at ASC LIMIT 15"
)

_QUERY_UNDER_CONNECTED = (
    "MATCH (n) WHERE NOT n:Domain AND NOT n:Insight "
    "AND (n.name IS NOT NULL OR n.title IS NOT NULL) "
    "AND n.status <> 'archived' "
    "WITH n, size([(n)-[]-() | 1]) AS rel_count "
    "WHERE rel_count < 2 "
    "RETURN coalesce(n.name, n.title) AS name, labels(n)[0] AS label, "
    "rel_count, n.created_at AS created "
    "ORDER BY n.created_at DESC LIMIT 15"
)


# ---------------------------------------------------------------------------
# Graph introspection query
# ---------------------------------------------------------------------------

_QUERY_GRAPH_PROFILE = (
    "MATCH (n) WHERE NOT n:Insight "
    "RETURN labels(n)[0] AS label, count(n) AS cnt "
    "ORDER BY cnt DESC"
)


# ---------------------------------------------------------------------------
# ReflectSkill
# ---------------------------------------------------------------------------


class ReflectSkill:
    """Adaptive cross-entity pattern detection skill.

    Inspects what's in the graph, selects applicable queries, filters
    dismissed Insights, and scores results by confidence.
    """

    def run(self, engine: "EngramaEngine") -> list[Insight]:
        """Execute adaptive detection and write Insight nodes.

        Returns:
            A list of :class:`Insight` instances that were created or updated.
        """
        # Step 1: Profile the graph
        profile = self._profile_graph(engine)

        # Step 2: Get dismissed insight titles to avoid re-surfacing
        dismissed = self._get_dismissed_titles(engine)

        # Step 3: Run applicable queries
        insights: list[Insight] = []

        # Activation conditions: only run queries where the graph has the
        # required node types.  Broadened in v0.5 to match real-world graphs.

        if profile.get("Problem") and profile.get("Project"):
            insights.extend(self._detect_cross_project_solutions(engine, dismissed))

        # shared_technology: any 2+ entities connected to Technology via USES/TEACHES
        if profile.get("Technology"):
            insights.extend(self._detect_shared_technology(engine, dismissed))

        # training_opportunity: Vulnerability OR open Problem sharing Concept with Course
        if (profile.get("Problem") or profile.get("Vulnerability")) and profile.get("Course"):
            insights.extend(self._detect_training_opportunities(engine, dismissed))

        if profile.get("Technique") and profile.get("Domain", 0) >= 2:
            insights.extend(self._detect_technique_transfer(engine, dismissed))

        if profile.get("Concept"):
            insights.extend(self._detect_concept_clustering(engine, dismissed))

        # stale_knowledge: any node connected to an active Project or Course
        if profile.get("Project") or profile.get("Course"):
            insights.extend(self._detect_stale_knowledge(engine, dismissed))

        # Under-connected always runs (useful for any graph)
        total_nodes = sum(profile.values())
        if total_nodes >= 5:
            insights.extend(self._detect_under_connected(engine, dismissed))

        return insights

    # ------------------------------------------------------------------
    # Graph introspection
    # ------------------------------------------------------------------

    @staticmethod
    def _profile_graph(engine: "EngramaEngine") -> dict[str, int]:
        """Return a dict of {label: count} for all node types in the graph."""
        records = engine._client.run(_QUERY_GRAPH_PROFILE, {})
        return {r["label"]: r["cnt"] for r in records}

    @staticmethod
    def _get_dismissed_titles(engine: "EngramaEngine") -> set[str]:
        """Return titles of all dismissed Insights to avoid re-surfacing."""
        records = engine._client.run(
            "MATCH (i:Insight {status: 'dismissed'}) RETURN i.title AS title",
            {},
        )
        return {r["title"] for r in records}

    # ------------------------------------------------------------------
    # Detection methods — original three (improved)
    # ------------------------------------------------------------------

    def _detect_cross_project_solutions(
        self, engine: "EngramaEngine", dismissed: set[str],
    ) -> list[Insight]:
        records = engine._client.run(
            _QUERY_CROSS_PROJECT_SOLUTION,
            {"open_status": "open", "resolved_status": "resolved"},
        )
        results: list[Insight] = []
        for r in records:
            title = (
                f"Solution transfer: {r['decision']} "
                f"({r['source_project']} → {r['target_project']})"
            )
            if title in dismissed:
                continue
            body = (
                f"The open problem \"{r['open_problem']}\" in project "
                f"\"{r['target_project']}\" shares the concept "
                f"\"{r['concept']}\" with a resolved problem in project "
                f"\"{r['source_project']}\". The decision "
                f"\"{r['decision']}\" may apply here."
            )
            insight = self._write_insight(
                engine, title=title, body=body,
                source_query="cross_project_solution", confidence=0.85,
            )
            results.append(insight)
        return results

    def _detect_shared_technology(
        self, engine: "EngramaEngine", dismissed: set[str],
    ) -> list[Insight]:
        records = engine._client.run(
            _QUERY_SHARED_TECHNOLOGY, {},
        )
        results: list[Insight] = []
        for r in records:
            a_desc = f"{r['type_a']}:{r['entity_a']}"
            b_desc = f"{r['type_b']}:{r['entity_b']}"
            title = (
                f"Shared technology: {r['technology']} "
                f"({a_desc} & {b_desc})"
            )
            if title in dismissed:
                continue
            # Cross-type sharing is more interesting than same-type
            confidence = 0.75 if r["type_a"] != r["type_b"] else 0.6
            body = (
                f"{a_desc} and {b_desc} both use {r['technology']}. "
                f"Consider sharing knowledge or materials between them."
            )
            insight = self._write_insight(
                engine, title=title, body=body,
                source_query="shared_technology", confidence=confidence,
            )
            results.append(insight)
        return results

    def _detect_training_opportunities(
        self, engine: "EngramaEngine", dismissed: set[str],
    ) -> list[Insight]:
        records = engine._client.run(
            _QUERY_TRAINING_OPPORTUNITY,
            {"open_status": "open"},
        )
        results: list[Insight] = []
        for r in records:
            issue_desc = f"{r['issue_type']}:{r['issue']}"
            title = (
                f"Training opportunity: {r['course']} "
                f"covers {r['concept']} (relates to: {issue_desc})"
            )
            if title in dismissed:
                continue
            body = (
                f"The {r['issue_type'].lower()} \"{r['issue']}\" involves "
                f"the concept \"{r['concept']}\", which is covered by the "
                f"course \"{r['course']}\". Reviewing this material may help."
            )
            insight = self._write_insight(
                engine, title=title, body=body,
                source_query="training_opportunity", confidence=0.65,
            )
            results.append(insight)
        return results

    # ------------------------------------------------------------------
    # Detection methods — new patterns (Phase 2)
    # ------------------------------------------------------------------

    def _detect_technique_transfer(
        self, engine: "EngramaEngine", dismissed: set[str],
    ) -> list[Insight]:
        """A Technique used in domain A could apply in domain B."""
        records = engine._client.run(_QUERY_TECHNIQUE_TRANSFER, {})
        results: list[Insight] = []
        for r in records:
            title = (
                f"Technique transfer: {r['technique']} "
                f"({r['source_domain']} → {r['target_domain']})"
            )
            if title in dismissed:
                continue
            related = r["related_entities"]
            confidence = min(0.5 + (related * 0.1), 0.9)
            body = (
                f"The technique \"{r['technique']}\" is used in "
                f"\"{r['source_domain']}\" but not in "
                f"\"{r['target_domain']}\". There are {related} "
                f"entities in {r['target_domain']} that share concepts "
                f"with this technique — it may be applicable there."
            )
            insight = self._write_insight(
                engine, title=title, body=body,
                source_query="technique_transfer", confidence=confidence,
            )
            results.append(insight)
        return results

    def _detect_concept_clustering(
        self, engine: "EngramaEngine", dismissed: set[str],
    ) -> list[Insight]:
        """Multiple unrelated entities share the same Concept."""
        records = engine._client.run(_QUERY_CONCEPT_CLUSTERING, {})
        results: list[Insight] = []
        for r in records:
            concept = r["concept"]
            count = r["entity_count"]
            sample = r["sample"]
            title = f"Concept cluster: {concept} ({count} entities)"
            if title in dismissed:
                continue
            sample_desc = ", ".join(
                f"{s['label']}:{s['name']}" for s in sample[:5]
            )
            confidence = min(0.5 + (count * 0.05), 0.9)
            body = (
                f"The concept \"{concept}\" connects {count} entities: "
                f"{sample_desc}. This cluster may reveal a pattern worth "
                f"exploring — these entities share a common thread."
            )
            insight = self._write_insight(
                engine, title=title, body=body,
                source_query="concept_clustering", confidence=confidence,
            )
            results.append(insight)
        return results

    def _detect_stale_knowledge(
        self, engine: "EngramaEngine", dismissed: set[str],
    ) -> list[Insight]:
        """Nodes connected to active Projects that are stale.

        Staleness criteria (DDR-003 Phase D):
        - Not updated in 90+ days, OR
        - Confidence below 0.3 (regardless of age).
        """
        records = engine._client.run(
            _QUERY_STALE_KNOWLEDGE, {"active_status": "active"},
        )
        results: list[Insight] = []
        for r in records:
            name = r["name"]
            title = (
                f"Stale knowledge: {r['label']}:{name} "
                f"(linked to {r['project']})"
            )
            if title in dismissed:
                continue
            last_updated = r["last_updated"]
            if hasattr(last_updated, "isoformat"):
                last_updated = last_updated.isoformat()[:10]
            confidence = r.get("confidence")
            conf_str = f" (confidence: {confidence:.2f})" if confidence is not None else ""
            # Determine staleness reason
            if confidence is not None and confidence < 0.3:
                reason = f"has low confidence ({confidence:.2f})"
            else:
                reason = f"hasn't been updated since {last_updated}"
            body = (
                f"The {r['label']} \"{name}\" is connected to the active "
                f"project \"{r['project']}\" via {r['rel']}, but {reason}{conf_str}. "
                f"Consider updating or archiving this node."
            )
            insight = self._write_insight(
                engine, title=title, body=body,
                source_query="stale_knowledge", confidence=0.5,
            )
            results.append(insight)
        return results

    # Stable title avoids uniqueness-constraint collisions when the node
    # count changes between runs (BUG-007).
    _UNDER_CONNECTED_TITLE = "Under-connected nodes need more relationships"

    def _detect_under_connected(
        self, engine: "EngramaEngine", dismissed: set[str],
    ) -> list[Insight]:
        """Nodes with fewer than 2 relationships — likely under-classified."""
        title = self._UNDER_CONNECTED_TITLE

        # BUG-007: skip if already dismissed (by stable title or source_query)
        if title in dismissed:
            return []
        dismissed_sq = engine._client.run(
            "MATCH (i:Insight {source_query: 'under_connected', status: 'dismissed'}) "
            "RETURN i.title AS title LIMIT 1",
            {},
        )
        if dismissed_sq:
            return []

        records = engine._client.run(_QUERY_UNDER_CONNECTED, {})
        if not records:
            return []

        # Build body with current counts (title stays stable)
        names = [f"{r['label']}:{r['name']}" for r in records[:10]]
        total = len(records)
        body = (
            f"Found {total} nodes with fewer than 2 relationships. "
            f"These are likely under-classified and would benefit from "
            f"adding INSTANCE_OF, BELONGS_TO, or IN_DOMAIN connections. "
            f"Top candidates: {', '.join(names)}."
        )

        # MERGE on stable title — idempotent, updates body on repeat runs
        insight = self._write_insight(
            engine, title=title, body=body,
            source_query="under_connected", confidence=0.4,
        )
        return [insight]

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _write_insight(
        engine: "EngramaEngine",
        *,
        title: str,
        body: str,
        source_query: str,
        confidence: float = 0.8,
    ) -> Insight:
        """Merge an Insight node into Neo4j and return the dataclass."""
        engine.merge_node("Insight", {
            "title": title,
            "body": body,
            "confidence": confidence,
            "status": "pending",
            "source_query": source_query,
        })
        return Insight(
            title=title,
            body=body,
            confidence=confidence,
            status="pending",
            source_query=source_query,
        )
