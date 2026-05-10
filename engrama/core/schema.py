"""
Engrama — Graph schema as Python dataclasses.

Auto-generated from profile: base+hacking+teaching+photography+ai
Generated at: 2026-04-12T15:47:30

Do not edit manually — regenerate with:
    python scripts/generate_from_profile.py profiles/base+hacking+teaching+photography+ai.yaml
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class NodeType(str, Enum):
    """Labels for every node defined in the base+hacking+teaching+photography+ai profile."""

    PROJECT = "Project"
    CONCEPT = "Concept"
    DECISION = "Decision"
    PROBLEM = "Problem"
    TECHNOLOGY = "Technology"
    PERSON = "Person"
    DOMAIN = "Domain"
    CLIENT = "Client"
    TARGET = "Target"
    VULNERABILITY = "Vulnerability"
    TECHNIQUE = "Technique"
    TOOL = "Tool"
    CTF = "CTF"
    COURSE = "Course"
    EXERCISE = "Exercise"
    MATERIAL = "Material"
    PHOTO = "Photo"
    LOCATION = "Location"
    SPECIES = "Species"
    GEAR = "Gear"
    MODEL = "Model"
    DATASET = "Dataset"
    EXPERIMENT = "Experiment"
    PIPELINE = "Pipeline"
    INSIGHT = "Insight"


class RelationType(str, Enum):
    """Relationship types defined in the base+hacking+teaching+photography+ai profile."""

    INSTANCE_OF = "INSTANCE_OF"
    COMPOSED_OF = "COMPOSED_OF"
    PERFORMS = "PERFORMS"
    SOLVED_BY = "SOLVED_BY"
    SERVES = "SERVES"
    BELONGS_TO = "BELONGS_TO"
    IN_DOMAIN = "IN_DOMAIN"
    USES = "USES"
    INFORMED_BY = "INFORMED_BY"
    HAS = "HAS"
    APPLIES = "APPLIES"
    IMPLEMENTS = "IMPLEMENTS"
    INVOLVES = "INVOLVES"
    FOR = "FOR"
    DEPENDS_ON = "DEPENDS_ON"
    SIMILAR_TO = "SIMILAR_TO"
    CAUSED_BY = "CAUSED_BY"
    REPLACES = "REPLACES"
    RELATED_TO = "RELATED_TO"
    SUBSET_OF = "SUBSET_OF"
    CONTRADICTS = "CONTRADICTS"
    LINKS_TO = "LINKS_TO"
    EXPLOITS = "EXPLOITS"
    EXECUTED_WITH = "EXECUTED_WITH"
    TARGETS = "TARGETS"
    DOCUMENTS = "DOCUMENTS"
    COVERS = "COVERS"
    TEACHES = "TEACHES"
    INCLUDES = "INCLUDES"
    ORIGIN_OF = "ORIGIN_OF"
    PRACTICES = "PRACTICES"
    REQUIRES = "REQUIRES"
    PREREQUISITE_OF = "PREREQUISITE_OF"
    HAS_MATERIAL = "HAS_MATERIAL"
    TAKEN_AT = "TAKEN_AT"
    FEATURES = "FEATURES"
    SHOT_WITH = "SHOT_WITH"
    INHABITS = "INHABITS"
    TRAINS_ON = "TRAINS_ON"
    RUNS = "RUNS"
    EVALUATES = "EVALUATES"
    FEEDS = "FEEDS"


# ---------------------------------------------------------------------------
# Node dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Project:
    """A project, product, or major initiative."""

    name: str
    status: str | None = None
    repo: str | None = None
    stack: list[str] = field(default_factory=list)
    description: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Concept:
    """A concept, idea, or knowledge area. The bridge between domains."""

    name: str
    domain: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Decision:
    """A decision with rationale and alternatives considered."""

    title: str
    rationale: str | None = None
    date: datetime.date | None = None
    status: str | None = None
    alternatives: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Problem:
    """A problem, challenge, or blocker encountered."""

    title: str
    solution: str | None = None
    status: str | None = None
    context: str | None = None
    severity: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Technology:
    """A language, framework, tool, or infrastructure component."""

    name: str
    version: str | None = None
    type: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Person:
    """A person — colleague, client, collaborator, or contact."""

    name: str
    role: str | None = None
    organisation: str | None = None
    contact: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Domain:
    """A field of knowledge — web-development, cybersecurity, cooking, photography."""

    name: str
    description: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Client:
    """An organisation that commissions work or training."""

    name: str
    sector: str | None = None
    contact: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Target:
    """A machine, network, or service being assessed."""

    name: str
    ip: str | None = None
    os: str | None = None
    status: str | None = None
    scope: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Vulnerability:
    """A vulnerability or misconfiguration found during assessment."""

    title: str
    cve: str | None = None
    severity: str | None = None
    status: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Technique:
    """An attack technique — maps to MITRE ATT&CK where applicable."""

    name: str
    mitre_id: str | None = None
    tactic: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Tool:
    """A security tool — scanner, exploit framework, utility."""

    name: str
    version: str | None = None
    type: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class CTF:
    """A CTF challenge or HackTheBox machine."""

    name: str
    platform: str | None = None
    difficulty: str | None = None
    status: str | None = None
    writeup_path: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Course:
    """A training course or workshop delivered."""

    name: str
    cohort: str | None = None
    date: datetime.date | None = None
    level: str | None = None
    status: str | None = None
    description: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Exercise:
    """A hands-on lab, exercise, or practical challenge."""

    title: str
    difficulty: str | None = None
    duration: str | None = None
    status: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Material:
    """A teaching artifact: cheatsheet, slides, exercise sheet, or reference card."""

    name: str
    type: str | None = None
    format: str | None = None
    status: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Photo:
    """A photograph or photo session."""

    title: str
    date: datetime.date | None = None
    location: str | None = None
    species: str | None = None
    camera: str | None = None
    lens: str | None = None
    status: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Location:
    """A geographic location — birding spot, nature reserve, trail."""

    name: str
    region: str | None = None
    coordinates: str | None = None
    habitat: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Species:
    """A species of bird, mammal, insect, or plant."""

    name: str
    family: str | None = None
    conservation_status: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Gear:
    """Camera body, lens, tripod, or other photography equipment."""

    name: str
    type: str | None = None
    brand: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Model:
    """An AI/ML model — LLM, classifier, embedding model, etc."""

    name: str
    type: str | None = None
    provider: str | None = None
    version: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Dataset:
    """A dataset used for training, evaluation, or analysis."""

    name: str
    source: str | None = None
    size: str | None = None
    format: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Experiment:
    """An ML experiment or evaluation run."""

    title: str
    status: str | None = None
    metric: str | None = None
    result: str | None = None
    date: datetime.date | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Pipeline:
    """A data or ML pipeline — preprocessing, training, inference."""

    name: str
    status: str | None = None
    steps: str | None = None
    notes: str | None = None
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass
class Insight:
    """A cross-entity pattern detected by the reflect skill."""

    title: str
    body: str = ""
    confidence: float = 0.8
    status: str = "pending"
    source_query: str = ""
    summary: str | None = None
    details: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

NODE_DATACLASS_MAP: dict[NodeType, type] = {
    NodeType.PROJECT: Project,
    NodeType.CONCEPT: Concept,
    NodeType.DECISION: Decision,
    NodeType.PROBLEM: Problem,
    NodeType.TECHNOLOGY: Technology,
    NodeType.PERSON: Person,
    NodeType.DOMAIN: Domain,
    NodeType.CLIENT: Client,
    NodeType.TARGET: Target,
    NodeType.VULNERABILITY: Vulnerability,
    NodeType.TECHNIQUE: Technique,
    NodeType.TOOL: Tool,
    NodeType.CTF: CTF,
    NodeType.COURSE: Course,
    NodeType.EXERCISE: Exercise,
    NodeType.MATERIAL: Material,
    NodeType.PHOTO: Photo,
    NodeType.LOCATION: Location,
    NodeType.SPECIES: Species,
    NodeType.GEAR: Gear,
    NodeType.MODEL: Model,
    NodeType.DATASET: Dataset,
    NodeType.EXPERIMENT: Experiment,
    NodeType.PIPELINE: Pipeline,
    NodeType.INSIGHT: Insight,
}
"""Maps each ``NodeType`` enum member to its corresponding dataclass."""


TITLE_KEYED_LABELS: frozenset[str] = frozenset(
    {"Experiment", "Vulnerability", "Decision", "Problem", "Exercise", "Photo"}
)
"""Node labels that use ``title`` instead of ``name`` as merge key."""
