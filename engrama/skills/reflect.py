"""
engrama/skills/reflect.py

Adaptive cross-entity pattern detection for the memory graph.

Instead of running a fixed set of hardcoded queries, the reflect skill:

1. **Inspects** the graph to see what labels and relationship types actually
   have data.
2. **Selects** applicable detection queries based on what's present.
3. **Filters** out Insights a human already approved or dismissed, so the
   user isn't re-bothered and a re-run never undoes a review.
4. **Scores** each Insight with a confidence value.

The detectors themselves (activation rules, queries, builders, per-run
budget) are declared once in :mod:`engrama.core.reflection` and shared with
the MCP ``engrama_reflect`` tool, so both produce the same Insights.

Detection patterns:

- **Cross-project solution transfer** — an open Problem shares a Concept with
  a resolved Problem that has a Decision.
- **Shared technology** — a Technology used by three or more live entities.
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

from typing import TYPE_CHECKING

from engrama.core.reflection import DETECTORS, InsightDraft, select
from engrama.core.schema import Insight

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine


class ReflectSkill:
    """Adaptive cross-entity pattern detection skill.

    Inspects what's in the caller's slice of the graph, runs the applicable
    detectors, skips already-judged Insights, and writes the rest.
    """

    def run(self, engine: EngramaEngine) -> list[Insight]:
        """Execute adaptive detection and write Insight nodes.

        Returns:
            A list of :class:`Insight` instances that were created or updated.
        """
        store = engine._store
        scope = engine.default_scope
        profile = store.count_labels(scope=scope)
        judged = store.get_dismissed_insight_titles(scope=scope) | (
            store.get_approved_insight_titles(scope=scope)
        )

        insights: list[Insight] = []
        for detector in DETECTORS:
            if not detector.applies(profile):
                continue
            if detector.name == "under_connected" and store.find_insight_by_source_query(
                "under_connected", statuses=["dismissed"], scope=scope
            ):
                continue
            records = getattr(store, detector.store_method)(scope=scope)
            for draft in select(detector.build(records), judged):
                insights.append(self._write_insight(engine, draft))
        return insights

    @staticmethod
    def _write_insight(engine: EngramaEngine, draft: InsightDraft) -> Insight:
        """Merge an Insight node and return the dataclass."""
        engine.merge_node("Insight", {"title": draft.title, **draft.properties()})
        return Insight(
            title=draft.title,
            body=draft.body,
            confidence=draft.confidence,
            status="pending",
            source_query=draft.source_query,
        )
