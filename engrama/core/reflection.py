"""Reflect detectors: when each one runs and how its rows become Insights.

Declared once and used by both reflect entry points — the MCP tool (async
store) and the SDK skill (sync store) — so they produce the same Insights
(DDR-008). Each detector names the store method that runs its query; the
queries themselves live in the stores (``_reflect_cypher`` for Neo4j, SQL for
SQLite) and only ever see live nodes in the caller's scope.

Builders are pure: rows in, :class:`InsightDraft` out. Titles are stable for a
given pattern (no counts in them), so a re-run updates the same Insight
instead of adding a near-duplicate, and :func:`select` drops titles a human
already judged and caps every detector at :data:`BUDGET_PER_DETECTOR`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

SHARED_TECHNOLOGY_MIN_MEMBERS = 3
BUDGET_PER_DETECTOR = 25
UNDER_CONNECTED_TITLE = "Under-connected nodes need more relationships"
_SAMPLE = 10


@dataclass(frozen=True)
class InsightDraft:
    """An Insight a detector wants written.

    ``about`` lists the ``(label, name)`` of the entities the Insight talks
    about, so it can be linked to them.
    """

    title: str
    body: str
    confidence: float
    source_query: str
    about: tuple[tuple[str, str], ...] = ()

    def about_targets(self) -> list[tuple[str, str, str]]:
        """``(label, key_field, name)`` for every entity to link with ``ABOUT``."""
        from engrama.core.schema import TITLE_KEYED_LABELS

        return [
            (label, "title" if label in TITLE_KEYED_LABELS else "name", name)
            for label, name in dict.fromkeys(self.about)
        ]

    def properties(self) -> dict[str, Any]:
        return {
            "body": self.body,
            "confidence": self.confidence,
            "status": "pending",
            "source_query": self.source_query,
        }


@dataclass(frozen=True)
class Detector:
    """One reflect pattern: activation conditions, store query, builder."""

    name: str
    store_method: str
    build: Callable[[list[dict[str, Any]]], list[InsightDraft]]
    required: tuple[str, ...] = ()
    any_of: tuple[tuple[str, ...], ...] = ()
    min_counts: tuple[tuple[str, int], ...] = ()
    min_total: int = 0

    def applies(self, profile: dict[str, int]) -> bool:
        """Whether the caller's graph has what this pattern needs."""
        if any(not profile.get(label) for label in self.required):
            return False
        if any(not any(profile.get(label) for label in group) for group in self.any_of):
            return False
        if any(profile.get(label, 0) < n for label, n in self.min_counts):
            return False
        return sum(profile.values()) >= self.min_total


def select(drafts: Iterable[InsightDraft], judged: set[str]) -> list[InsightDraft]:
    """Drop already-judged and repeated titles, then apply the budget."""
    out: list[InsightDraft] = []
    seen: set[str] = set()
    for draft in drafts:
        if draft.title in judged or draft.title in seen:
            continue
        seen.add(draft.title)
        out.append(draft)
        if len(out) >= BUDGET_PER_DETECTOR:
            break
    return out


def _ref(entity: dict[str, Any]) -> str:
    return f"{entity['label']}:{entity['name']}"


def _pairs(entities: Iterable[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple((e["label"], e["name"]) for e in entities if e.get("label") and e.get("name"))


def _cross_project(records: list[dict[str, Any]]) -> list[InsightDraft]:
    return [
        InsightDraft(
            title=(
                f"Solution transfer: {r['decision']} "
                f"({r['source_project']} → {r['target_project']})"
            ),
            body=(
                f'The open problem "{r["open_problem"]}" in project '
                f'"{r["target_project"]}" shares the concept "{r["concept"]}" with a '
                f'resolved problem in project "{r["source_project"]}". The decision '
                f'"{r["decision"]}" may apply here.'
            ),
            confidence=0.85,
            source_query="cross_project_solution",
            about=(
                ("Decision", r["decision"]),
                ("Problem", r["open_problem"]),
                ("Project", r["target_project"]),
                ("Project", r["source_project"]),
            ),
        )
        for r in records
    ]


def _shared_technology(records: list[dict[str, Any]]) -> list[InsightDraft]:
    drafts = []
    for r in records:
        members = sorted(r["members"], key=_ref)
        shown = ", ".join(_ref(m) for m in members[:_SAMPLE])
        more = f" and {len(members) - _SAMPLE} more" if len(members) > _SAMPLE else ""
        cross_type = len({m["label"] for m in members}) > 1
        drafts.append(
            InsightDraft(
                title=f"Shared technology: {r['technology']}",
                body=(
                    f"{len(members)} entities use {r['technology']}: {shown}{more}. "
                    "Consider sharing knowledge or materials between them."
                ),
                confidence=0.75 if cross_type else 0.6,
                source_query="shared_technology",
                about=(("Technology", r["technology"]), *_pairs(members)),
            )
        )
    return drafts


def _training(records: list[dict[str, Any]]) -> list[InsightDraft]:
    return [
        InsightDraft(
            title=(
                f"Training opportunity: {r['course']} covers {r['concept']} "
                f"(relates to: {r['issue_type']}:{r['issue']})"
            ),
            body=(
                f'The {r["issue_type"].lower()} "{r["issue"]}" involves the concept '
                f'"{r["concept"]}", which is covered by the course "{r["course"]}". '
                "Reviewing this material may help."
            ),
            confidence=0.65,
            source_query="training_opportunity",
            about=(
                ("Course", r["course"]),
                ("Concept", r["concept"]),
                (r["issue_type"], r["issue"]),
            ),
        )
        for r in records
    ]


def _technique_transfer(records: list[dict[str, Any]]) -> list[InsightDraft]:
    return [
        InsightDraft(
            title=(
                f"Technique transfer: {r['technique']} "
                f"({r['source_domain']} → {r['target_domain']})"
            ),
            body=(
                f'The technique "{r["technique"]}" is used in "{r["source_domain"]}" but '
                f'not in "{r["target_domain"]}". {r["related_entities"]} entities in '
                f"{r['target_domain']} share concepts with it, so it may apply there."
            ),
            confidence=min(0.5 + r["related_entities"] * 0.1, 0.9),
            source_query="technique_transfer",
            about=(
                ("Technique", r["technique"]),
                ("Domain", r["source_domain"]),
                ("Domain", r["target_domain"]),
            ),
        )
        for r in records
    ]


def _concept_clusters(records: list[dict[str, Any]]) -> list[InsightDraft]:
    drafts = []
    for r in records:
        sample = list(r.get("sample") or [])[:5]
        drafts.append(
            InsightDraft(
                title=f"Concept cluster: {r['concept']}",
                body=(
                    f'The concept "{r["concept"]}" connects {r["entity_count"]} entities, '
                    f"including {', '.join(_ref(s) for s in sample)}. "
                    "This cluster may reveal a pattern worth exploring."
                ),
                confidence=min(0.5 + r["entity_count"] * 0.05, 0.9),
                source_query="concept_clustering",
                about=(("Concept", r["concept"]), *_pairs(sample)),
            )
        )
    return drafts


def _stale(records: list[dict[str, Any]]) -> list[InsightDraft]:
    drafts = []
    for r in records:
        last_updated = r.get("last_updated")
        if hasattr(last_updated, "isoformat"):
            last_updated = last_updated.isoformat()
        confidence = r.get("confidence")
        if confidence is not None and float(confidence) < 0.3:
            reason = f"has low confidence ({float(confidence):.2f})"
        else:
            reason = f"hasn't been updated since {str(last_updated)[:10]}"
        drafts.append(
            InsightDraft(
                title=f"Stale knowledge: {r['label']}:{r['name']} (linked to {r['project']})",
                body=(
                    f'The {r["label"]} "{r["name"]}" is connected to the active project '
                    f'"{r["project"]}" via {r["rel"]}, but {reason}. '
                    "Consider reviewing or archiving it."
                ),
                confidence=0.5,
                source_query="stale_knowledge",
                about=((r["label"], r["name"]),),
            )
        )
    return drafts


def _under_connected(records: list[dict[str, Any]]) -> list[InsightDraft]:
    if not records:
        return []
    isolated = sum(1 for r in records if not r.get("rel_count"))
    sample = records[:_SAMPLE]
    return [
        InsightDraft(
            title=UNDER_CONNECTED_TITLE,
            body=(
                f"Found {len(records)} nodes with fewer than 2 relationships "
                f"({isolated} with none). Linking them (INSTANCE_OF, BELONGS_TO, "
                f"IN_DOMAIN…) makes them reachable. Most isolated: "
                f"{', '.join(_ref(r) for r in sample)}."
            ),
            confidence=0.4,
            source_query="under_connected",
            about=_pairs(sample),
        )
    ]


DETECTORS: tuple[Detector, ...] = (
    Detector(
        "cross_project_solution",
        "detect_cross_project_solutions",
        _cross_project,
        required=("Problem", "Project"),
    ),
    Detector(
        "shared_technology",
        "detect_shared_technology",
        _shared_technology,
        required=("Technology",),
    ),
    Detector(
        "training_opportunity",
        "detect_training_opportunities",
        _training,
        any_of=(("Problem", "Vulnerability"), ("Course",)),
    ),
    Detector(
        "technique_transfer",
        "detect_technique_transfer",
        _technique_transfer,
        required=("Technique",),
        min_counts=(("Domain", 2),),
    ),
    Detector(
        "concept_clustering",
        "detect_concept_clusters",
        _concept_clusters,
        required=("Concept",),
    ),
    Detector(
        "stale_knowledge",
        "detect_stale_knowledge",
        _stale,
        any_of=(("Project", "Course"),),
    ),
    Detector(
        "under_connected",
        "detect_under_connected_nodes",
        _under_connected,
        min_total=5,
    ),
)


__all__ = [
    "BUDGET_PER_DETECTOR",
    "DETECTORS",
    "SHARED_TECHNOLOGY_MIN_MEMBERS",
    "UNDER_CONNECTED_TITLE",
    "Detector",
    "InsightDraft",
    "select",
]
