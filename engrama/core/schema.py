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
from typing import Optional


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
    status: Optional[str] = None
    repo: Optional[str] = None
    stack: list[str] = field(default_factory=list)
    description: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Concept:
    """A concept, idea, or knowledge area. The bridge between domains."""

    name: str
    domain: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Decision:
    """A decision with rationale and alternatives considered."""

    title: str
    rationale: Optional[str] = None
    date: Optional[datetime.date] = None
    status: Optional[str] = None
    alternatives: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Problem:
    """A problem, challenge, or blocker encountered."""

    title: str
    solution: Optional[str] = None
    status: Optional[str] = None
    context: Optional[str] = None
    severity: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Technology:
    """A language, framework, tool, or infrastructure component."""

    name: str
    version: Optional[str] = None
    type: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Person:
    """A person — colleague, client, collaborator, or contact."""

    name: str
    role: Optional[str] = None
    organisation: Optional[str] = None
    contact: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Domain:
    """A field of knowledge — web-development, cybersecurity, cooking, photography."""

    name: str
    description: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Client:
    """An organisation that commissions work or training."""

    name: str
    sector: Optional[str] = None
    contact: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Target:
    """A machine, network, or service being assessed."""

    name: str
    ip: Optional[str] = None
    os: Optional[str] = None
    status: Optional[str] = None
    scope: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Vulnerability:
    """A vulnerability or misconfiguration found during assessment."""

    title: str
    cve: Optional[str] = None
    severity: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Technique:
    """An attack technique — maps to MITRE ATT&CK where applicable."""

    name: str
    mitre_id: Optional[str] = None
    tactic: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Tool:
    """A security tool — scanner, exploit framework, utility."""

    name: str
    version: Optional[str] = None
    type: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class CTF:
    """A CTF challenge or HackTheBox machine."""

    name: str
    platform: Optional[str] = None
    difficulty: Optional[str] = None
    status: Optional[str] = None
    writeup_path: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Course:
    """A training course or workshop delivered."""

    name: str
    cohort: Optional[str] = None
    date: Optional[datetime.date] = None
    level: Optional[str] = None
    status: Optional[str] = None
    description: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Exercise:
    """A hands-on lab, exercise, or practical challenge."""

    title: str
    difficulty: Optional[str] = None
    duration: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Material:
    """A teaching artifact: cheatsheet, slides, exercise sheet, or reference card."""

    name: str
    type: Optional[str] = None
    format: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Photo:
    """A photograph or photo session."""

    title: str
    date: Optional[datetime.date] = None
    location: Optional[str] = None
    species: Optional[str] = None
    camera: Optional[str] = None
    lens: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Location:
    """A geographic location — birding spot, nature reserve, trail."""

    name: str
    region: Optional[str] = None
    coordinates: Optional[str] = None
    habitat: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Species:
    """A species of bird, mammal, insect, or plant."""

    name: str
    family: Optional[str] = None
    conservation_status: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Gear:
    """Camera body, lens, tripod, or other photography equipment."""

    name: str
    type: Optional[str] = None
    brand: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Model:
    """An AI/ML model — LLM, classifier, embedding model, etc."""

    name: str
    type: Optional[str] = None
    provider: Optional[str] = None
    version: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Dataset:
    """A dataset used for training, evaluation, or analysis."""

    name: str
    source: Optional[str] = None
    size: Optional[str] = None
    format: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Experiment:
    """An ML experiment or evaluation run."""

    title: str
    status: Optional[str] = None
    metric: Optional[str] = None
    result: Optional[str] = None
    date: Optional[datetime.date] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Pipeline:
    """A data or ML pipeline — preprocessing, training, inference."""

    name: str
    status: Optional[str] = None
    steps: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class Insight:
    """A cross-entity pattern detected by the reflect skill."""

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


TITLE_KEYED_LABELS: frozenset[str] = frozenset({'Experiment', 'Vulnerability', 'Decision', 'Problem', 'Exercise', 'Photo'})
"""Node labels that use ``title`` instead of ``name`` as merge key."""
