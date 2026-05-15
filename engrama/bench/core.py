"""Core benchmark primitives.

Defines the shapes every dataset-specific loader emits so the runner
(PR-G3) and reporter (PR-G4) can be written once against a normalised
contract. Loaders translate from their native JSON layout into these.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BenchmarkTurn:
    """A single message in a conversation transcript.

    ``speaker`` is the role label produced by the source dataset
    (e.g. ``"Caroline"`` or ``"user"``); the loader does not normalise
    it because some scoring strategies key on the original speaker name.
    """

    speaker: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkConversation:
    """One conversation = ordered list of sessions, each a list of turns.

    LOCOMO ships ~600-turn conversations split into multiple sessions
    (sessions correspond to distinct dialogue events in time). LongMemEval
    is similar. Keeping sessions explicit lets the runner inject one
    session at a time and query in between if a scenario demands it.
    """

    conversation_id: str
    sessions: list[list[BenchmarkTurn]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkQuestion:
    """One question/answer pair tied to a conversation.

    ``evidence`` is the dataset's pointer to the turns that justify the
    answer — used by recall-style scoring (did engrama surface the
    relevant turns?). For LOCOMO each evidence entry is a string like
    ``"D5:12"`` meaning session 5, turn 12. Loaders preserve the native
    format here; normalisation is a scoring concern.
    """

    question_id: str
    conversation_id: str
    question: str
    expected_answer: str
    category: str | None = None
    evidence: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class Benchmark(ABC):
    """Abstract base for benchmark datasets.

    Subclasses parse a native source format and produce a normalised
    stream of :class:`BenchmarkConversation` + :class:`BenchmarkQuestion`.
    The loader is lazy: ``load(path)`` only reads the file; iteration
    is on-demand so a multi-GB dataset stays out of memory if the caller
    just wants a count.
    """

    name: str = ""

    #: How the runner (PR-G3) should partition the work for this dataset:
    #:
    #: * ``"per-conversation"`` — replay all of a conversation's sessions
    #:   into one fresh DB, then iterate all of *its* questions before
    #:   moving on. Right for LOCOMO, where every question is asked over
    #:   the same multi-session conversation.
    #: * ``"per-question"`` — replay the question's haystack into a fresh
    #:   DB and ask just that one question. Right for LongMemEval, where
    #:   each record ships its own self-contained haystack.
    lifecycle: str = "per-conversation"

    def __init__(self) -> None:
        self._raw: Any = None
        self._path: Path | None = None

    @abstractmethod
    def load(self, path: str | Path) -> None:
        """Parse the dataset file/directory at ``path`` into memory."""

    @abstractmethod
    def iter_conversations(self) -> Iterator[BenchmarkConversation]:
        """Yield every conversation in stable order."""

    @abstractmethod
    def iter_questions(self) -> Iterator[BenchmarkQuestion]:
        """Yield every question across every conversation in stable order."""

    def conversation_count(self) -> int:
        """Total conversations — convenience for the ``bench list`` CLI."""
        return sum(1 for _ in self.iter_conversations())

    def question_count(self) -> int:
        """Total questions across all conversations."""
        return sum(1 for _ in self.iter_questions())


def _read_json(path: str | Path) -> Any:
    """Read a JSON file as UTF-8 — small helper so loaders stay short."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


__all__ = [
    "Benchmark",
    "BenchmarkConversation",
    "BenchmarkQuestion",
    "BenchmarkTurn",
    "_read_json",
]
