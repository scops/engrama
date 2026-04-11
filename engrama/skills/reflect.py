"""
engrama/skills/reflect.py

The reflect skill traverses the Neo4j memory graph looking for cross-entity
patterns and writes :class:`~engrama.core.schema.Insight` nodes.

This is what makes Engrama distinctive: it detects connections that neither
the Obsidian narrative layer nor any single query could surface alone.

Three detection queries are executed:

1. **Cross-project solution transfer** — an open Problem in project B shares
   a Concept with a resolved Problem in project A that has a Decision.
2. **Shared technology** — two active Projects use the same Technology.
3. **Training opportunity** — an open Problem shares a Concept with a Course.

Each result row is written as an ``Insight`` node with ``status: "pending"``.
The human reviews and approves or dismisses.  Insights are never acted upon
automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engrama.core.schema import Insight

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine


# ---------------------------------------------------------------------------
# Detection query definitions
# ---------------------------------------------------------------------------

_QUERY_CROSS_PROJECT_SOLUTION = (
    "MATCH (pB:Project)-[:HAS]->(open:Problem {status: $open_status}), "
    "(open)-[:APPLIES]->(c:Concept)<-[:APPLIES]-(resolved:Problem {status: $resolved_status}), "
    "(resolved)-[:SOLVED_BY]->(d:Decision)<-[:INFORMED_BY]-(pA:Project) "
    "WHERE pA <> pB "
    "RETURN pB.name AS target_project, open.title AS open_problem, "
    "d.title AS decision, pA.name AS source_project, c.name AS concept"
)

_QUERY_SHARED_TECHNOLOGY = (
    "MATCH (pA:Project {status: $active_status})-[:USES]->(t:Technology)"
    "<-[:USES]-(pB:Project {status: $active_status}) "
    "WHERE id(pA) < id(pB) "
    "RETURN pA.name AS project_a, pB.name AS project_b, t.name AS technology"
)

_QUERY_TRAINING_OPPORTUNITY = (
    "MATCH (open:Problem {status: $open_status})-[:APPLIES]->(c:Concept)"
    "<-[:COVERS]-(course:Course) "
    "RETURN open.title AS problem, c.name AS concept, course.name AS course"
)


# ---------------------------------------------------------------------------
# ReflectSkill
# ---------------------------------------------------------------------------


class ReflectSkill:
    """Cross-entity pattern detection skill.

    Traverses the memory graph to find patterns across projects, problems,
    decisions, technologies, and courses.  Each detected pattern is written
    as an :class:`Insight` node via ``engine.merge_node()``.
    """

    def run(self, engine: "EngramaEngine") -> list[Insight]:
        """Execute all detection queries and write Insight nodes.

        Args:
            engine: An initialised :class:`EngramaEngine` connected to Neo4j.

        Returns:
            A list of :class:`Insight` instances that were created or updated.
        """
        insights: list[Insight] = []
        insights.extend(self._detect_cross_project_solutions(engine))
        insights.extend(self._detect_shared_technology(engine))
        insights.extend(self._detect_training_opportunities(engine))
        return insights

    # ------------------------------------------------------------------
    # Detection methods
    # ------------------------------------------------------------------

    def _detect_cross_project_solutions(self, engine: "EngramaEngine") -> list[Insight]:
        """Query 1 — a solution from project A may apply to project B.

        Finds open Problems that share a Concept with a resolved Problem
        that has a Decision, where the two Problems belong to different
        Projects.
        """
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
            body = (
                f"The open problem \"{r['open_problem']}\" in project "
                f"\"{r['target_project']}\" shares the concept "
                f"\"{r['concept']}\" with a resolved problem in project "
                f"\"{r['source_project']}\". The decision "
                f"\"{r['decision']}\" may apply here."
            )
            insight = self._write_insight(
                engine,
                title=title,
                body=body,
                source_query="cross_project_solution",
            )
            results.append(insight)
        return results

    def _detect_shared_technology(self, engine: "EngramaEngine") -> list[Insight]:
        """Query 2 — two active projects use the same technology.

        Useful to surface collaboration opportunities between projects.
        """
        records = engine._client.run(
            _QUERY_SHARED_TECHNOLOGY,
            {"active_status": "active"},
        )
        results: list[Insight] = []
        for r in records:
            title = (
                f"Shared technology: {r['technology']} "
                f"({r['project_a']} & {r['project_b']})"
            )
            body = (
                f"Both \"{r['project_a']}\" and \"{r['project_b']}\" "
                f"use {r['technology']}. Consider sharing knowledge, "
                f"libraries, or configuration between these projects."
            )
            insight = self._write_insight(
                engine,
                title=title,
                body=body,
                source_query="shared_technology",
                confidence=0.7,
            )
            results.append(insight)
        return results

    def _detect_training_opportunities(self, engine: "EngramaEngine") -> list[Insight]:
        """Query 3 — an open Problem relates to a concept taught in a Course.

        Useful to surface relevant training material for active problems.
        """
        records = engine._client.run(
            _QUERY_TRAINING_OPPORTUNITY,
            {"open_status": "open"},
        )
        results: list[Insight] = []
        for r in records:
            title = (
                f"Training opportunity: {r['course']} "
                f"covers {r['concept']} (relates to: {r['problem']})"
            )
            body = (
                f"The open problem \"{r['problem']}\" involves the concept "
                f"\"{r['concept']}\", which is covered by the course "
                f"\"{r['course']}\". Reviewing this material may help."
            )
            insight = self._write_insight(
                engine,
                title=title,
                body=body,
                source_query="training_opportunity",
                confidence=0.6,
            )
            results.append(insight)
        return results

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
        """Merge an Insight node into Neo4j and return the dataclass.

        Args:
            engine: The Engrama engine instance.
            title: Unique insight title.
            body: Human-readable description.
            source_query: Identifier for the detection pattern.
            confidence: Confidence score (0.0–1.0).

        Returns:
            An :class:`Insight` instance representing the merged node.
        """
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
