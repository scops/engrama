"""Benchmark scoring strategies (Roadmap P15 / DDR-003 Part 7).

PR-G3 ships the deterministic, no-LLM recall-based scorer that the
runner uses by default. It compares the set of node identifiers
returned by engrama against the dataset's ``evidence`` pointer for
each question and reports ``recall@k``.

LLM-as-judge is intentionally stubbed (``NotImplementedError``): the
plumbing is here so PR-G4 (or a follow-up) can drop in a judge backend
without breaking the runner's contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RetrievalRun:
    """The runner's per-question output handed to a :class:`Scorer`.

    Kept tiny on purpose so a future scorer (LLM judge, model-graded
    answer, etc.) can plug in without the runner needing to know what
    fields it cares about.
    """

    question_id: str
    expected_evidence: list[str]
    retrieved_ids: list[str]
    retrieved_names: list[str]
    answer_text: str | None = None


@dataclass(frozen=True)
class ScoreReport:
    """One row in the per-question results array.

    ``metric`` names the scorer that produced ``score`` (e.g.
    ``"recall@5"``) so an aggregator can spot mixed runs.
    """

    metric: str
    score: float
    matched: list[str]
    missed: list[str]


class Scorer(Protocol):
    """Strategy interface for benchmark scoring."""

    metric: str

    def score(self, run: RetrievalRun) -> ScoreReport: ...


class RecallAtK:
    """Recall-based scorer matching retrieved identifiers vs evidence.

    A question's *evidence* is a list of identifiers the dataset
    guarantees justify the expected answer (e.g. LOCOMO's ``"D5:12"``
    dia ids, LongMemEval's session ids). The recall is the fraction of
    expected items that appear among the first ``k`` retrieved items.

    ``k`` is captured in :attr:`metric` so a mixed report makes the cap
    obvious (``recall@5`` vs ``recall@10``). Matching is done against
    both retrieved node identifiers (``node_id``) and node *names* — the
    runner emits both because the dataset's evidence pointer can resolve
    against either depending on how the runner stored the turn (name =
    ``dia_id`` for LOCOMO, name = session id for LongMemEval).

    Questions with empty evidence are treated as a *trivial pass*
    (score 1.0) — they signal the dataset itself cannot grade retrieval
    for that row, so penalising the engine would punish a no-op. The
    aggregate report keeps the count of these separately so the headline
    number doesn't get inflated.
    """

    def __init__(self, k: int = 5) -> None:
        if k <= 0:
            raise ValueError(f"k must be positive; got {k}")
        self.k = k
        self.metric = f"recall@{k}"

    def score(self, run: RetrievalRun) -> ScoreReport:
        expected = list(run.expected_evidence)
        if not expected:
            return ScoreReport(metric=self.metric, score=1.0, matched=[], missed=[])

        top_ids = run.retrieved_ids[: self.k]
        top_names = run.retrieved_names[: self.k]
        candidates = [*top_ids, *top_names]

        def _matches(ev: str, candidate: str) -> bool:
            # Exact match works for dataset formats where the evidence
            # pointer maps 1:1 to a turn id (LOCOMO's ``D1:1``). Prefix
            # match (with a path-style separator) covers the case where
            # the dataset's evidence is broader than a single turn
            # (LongMemEval's session-level ``s2`` matching a turn named
            # ``s2:0``).
            if candidate == ev:
                return True
            return candidate.startswith(f"{ev}:") or candidate.startswith(f"{ev}/")

        matched: list[str] = []
        missed: list[str] = []
        for ev in expected:
            if any(_matches(ev, c) for c in candidates):
                matched.append(ev)
            else:
                missed.append(ev)
        recall = len(matched) / len(expected)
        return ScoreReport(metric=self.metric, score=recall, matched=matched, missed=missed)


class LLMJudge:
    """Placeholder for an LLM-as-judge scorer.

    The runner accepts any :class:`Scorer`, so a real implementation can
    land in a later PR without touching the runner. Until then,
    constructing the judge raises so a typo on the CLI fails loudly.
    """

    metric = "llm-judge"

    def __init__(self, *_: object, **__: object) -> None:
        raise NotImplementedError(
            "LLM-as-judge scoring is not implemented yet — "
            "use a recall-based scorer (e.g. `--scorer recall@5`)."
        )

    def score(self, run: RetrievalRun) -> ScoreReport:  # pragma: no cover - guard
        raise NotImplementedError


def build_scorer(spec: str) -> Scorer:
    """Resolve a CLI-friendly spec string into a :class:`Scorer`.

    Supported specs:

    * ``"recall@K"`` for any positive integer ``K``.
    * ``"llm-judge"`` (raises ``NotImplementedError`` for now).
    """
    spec = spec.strip().lower()
    if spec.startswith("recall@"):
        try:
            k = int(spec.split("@", 1)[1])
        except (IndexError, ValueError) as exc:
            raise ValueError(f"Invalid recall scorer spec: {spec!r}") from exc
        return RecallAtK(k=k)
    if spec == "llm-judge":
        return LLMJudge()
    raise ValueError(f"Unknown scorer: {spec!r}. Known: recall@K (K positive int), llm-judge.")


__all__ = [
    "LLMJudge",
    "RecallAtK",
    "RetrievalRun",
    "ScoreReport",
    "Scorer",
    "build_scorer",
]
