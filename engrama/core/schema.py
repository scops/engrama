"""
Engrama — Graph schema as Python dataclasses.

Maps every node type and relationship type from the ``developer`` profile
(see GRAPH-SCHEMA.md) into typed dataclasses and enums so the rest of the
codebase can work with structured objects instead of raw dicts.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class NodeType(str, Enum):
    """Labels for every node defined in the developer profile."""

    PROJECT = "Project"
    TECHNOLOGY = "Technology"
    DECISION = "Decision"
    PROBLEM = "Problem"
    COURSE = "Course"
    CONCEPT = "Concept"
    CLIENT = "Client"
    INSIGHT = "Insight"


class RelationType(str, Enum):
    """Relationship types defined in the developer profile."""

    USES = "USES"
    INFORMED_BY = "INFORMED_BY"
    HAS = "HAS"
    FOR = "FOR"
    ORIGIN_OF = "ORIGIN_OF"
    APPLIES = "APPLIES"
    SOLVED_BY = "SOLVED_BY"
    COVERS = "COVERS"
    TEACHES = "TEACHES"
    IMPLEMENTS = "IMPLEMENTS"


# ---------------------------------------------------------------------------
# Node dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Project:
    """A software project tracked in the developer's memory graph.

    Attributes:
        name: Unique project identifier (required).
        status: Lifecycle state — ``"active"``, ``"paused"``, or ``"archived"``.
        repo: Repository URL or path.
        stack: List of technology names used by the project.
        description: Free-text description of the project.
        created_at: Timestamp set automatically on first write.
        updated_at: Timestamp refreshed on every write.
    """

    name: str
    status: Optional[str] = None
    repo: Optional[str] = None
    stack: list[str] = field(default_factory=list)
    description: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Technology:
    """A technology, framework, language, or tool.

    Attributes:
        name: Unique technology name (required).
        version: Current or pinned version string.
        type: Category — ``"framework"``, ``"infra"``, ``"language"``,
              ``"protocol"``, or ``"tool"``.
        notes: Free-text notes.
        created_at: Timestamp set automatically on first write.
        updated_at: Timestamp refreshed on every write.
    """

    name: str
    version: Optional[str] = None
    type: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Decision:
    """An architectural or technical decision.

    Attributes:
        title: Unique decision title (required).
        rationale: Explanation of *why* this decision was made.
        date: Date the decision was recorded.
        alternatives: Alternatives considered (free text).
        created_at: Timestamp set automatically on first write.
        updated_at: Timestamp refreshed on every write.
    """

    title: str
    rationale: Optional[str] = None
    date: Optional[datetime.date] = None
    alternatives: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Problem:
    """A problem or blocker encountered during development.

    Attributes:
        title: Unique problem title (required).
        solution: Description of how it was (or should be) resolved.
        status: Current state — ``"open"``, ``"resolved"``, or ``"blocked"``.
        context: Extra context about the environment or circumstances.
        created_at: Timestamp set automatically on first write.
        updated_at: Timestamp refreshed on every write.
    """

    title: str
    solution: Optional[str] = None
    status: Optional[str] = None
    context: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Course:
    """A training course or workshop delivered by the instructor.

    Attributes:
        name: Unique course name (required).
        cohort: Cohort or group identifier.
        date: Date the course was delivered.
        level: Difficulty — ``"basic"``, ``"intermediate"``, or ``"advanced"``.
        client: Name of the organisation that commissioned the course.
        created_at: Timestamp set automatically on first write.
        updated_at: Timestamp refreshed on every write.
    """

    name: str
    cohort: Optional[str] = None
    date: Optional[datetime.date] = None
    level: Optional[str] = None
    client: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Concept:
    """A domain concept or knowledge area.

    Attributes:
        name: Unique concept name (required).
        domain: Knowledge domain this concept belongs to.
        notes: Free-text notes.
        created_at: Timestamp set automatically on first write.
        updated_at: Timestamp refreshed on every write.
    """

    name: str
    domain: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Client:
    """An organisation or individual that commissions work or training.

    Attributes:
        name: Unique client name (required).
        sector: Industry sector.
        contact: Primary contact information.
        created_at: Timestamp set automatically on first write.
        updated_at: Timestamp refreshed on every write.
    """

    name: str
    sector: Optional[str] = None
    contact: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Insight:
    """A cross-entity pattern detected by the reflect skill.

    Insights are proposed by the engine and approved by the human.
    They are never acted upon automatically.

    Attributes:
        title: Unique insight title (required).
        body: Human-readable description of the detected pattern.
        confidence: Confidence score between 0.0 and 1.0.
        status: Lifecycle state — ``"pending"``, ``"approved"``, or ``"dismissed"``.
        source_query: Name or identifier of the Cypher pattern that detected this.
        created_at: Timestamp set automatically on first write.
        updated_at: Timestamp refreshed on every write.
    """

    title: str
    body: str = ""
    confidence: float = 0.8
    status: str = "pending"
    source_query: str = ""
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

NODE_DATACLASS_MAP: dict[NodeType, type] = {
    NodeType.PROJECT: Project,
    NodeType.TECHNOLOGY: Technology,
    NodeType.DECISION: Decision,
    NodeType.PROBLEM: Problem,
    NodeType.COURSE: Course,
    NodeType.CONCEPT: Concept,
    NodeType.CLIENT: Client,
    NodeType.INSIGHT: Insight,
}
"""Maps each ``NodeType`` enum member to its corresponding dataclass."""

TITLE_KEYED_LABELS: frozenset[str] = frozenset({"Decision", "Problem"})
"""Node labels that use ``title`` instead of ``name`` as merge key."""
