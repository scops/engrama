"""Benchmark runner (Roadmap P15 / DDR-003 Part 7).

Replays a benchmark dataset into a temporary engrama database, runs
each question against it, and emits a JSON report consumable by the
reporter (PR-G4).

Design:

* Sync runner. Async only matters once we want multi-worker — out of
  scope for PR-G3.
* DB lifecycle is dictated by the loader's :attr:`Benchmark.lifecycle`
  attribute: ``per-conversation`` (LOCOMO) or ``per-question``
  (LongMemEval). Each cycle gets its own SQLite file under a temp
  directory so questions can't contaminate each other.
* Every run is tagged with a unique :class:`MemoryScope` (random
  ``session_id``) so concurrent runs against the same backing store
  can't bleed into one another either.
* Turn timestamps come from the dataset's ``session_dates`` /
  ``haystack_dates`` and are written as ``valid_from`` so engrama's
  temporal scoring (``temporal_gamma``) has real ground to stand on.
* Retrieval uses :meth:`Engrama.search` (fulltext) — deterministic,
  doesn't depend on an embedding provider, and matches what the
  scorer compares against.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engrama.bench.core import (
    Benchmark,
    BenchmarkConversation,
    BenchmarkQuestion,
    BenchmarkTurn,
)
from engrama.bench.scoring import (
    RecallAtK,
    RetrievalRun,
    Scorer,
    ScoreReport,
    build_scorer,
)

if TYPE_CHECKING:
    from engrama.adapters.sdk import Engrama

logger = logging.getLogger("engrama.bench.runner")

# Label used for every benchmark turn. ``Concept`` is in the default
# schema's whitelist (and therefore passes the sanitiser) and carries no
# domain-specific meaning that would confuse a reader inspecting the DB.
_TURN_LABEL: str = "Concept"

# Top-k for retrieval — runner-side. The scorer's ``k`` can be smaller
# (or equal). We pull a wider net so a recall@5 scorer sees the same
# ranking the engine would surface to a user.
_DEFAULT_RETRIEVAL_LIMIT: int = 10


# ---------------------------------------------------------------------------
# Result rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionResult:
    """One row in the report's ``questions`` array."""

    question_id: str
    conversation_id: str
    category: str | None
    expected_evidence: list[str]
    retrieved_ids: list[str]
    retrieved_names: list[str]
    score: float
    matched: list[str]
    missed: list[str]
    latency_ms: float


@dataclass
class BenchmarkReport:
    """In-memory mirror of the report JSON the CLI writes.

    Kept as a separate dataclass so callers (tests, future bench
    aggregators) can keep the rich structure without forcing a JSON
    round-trip. :meth:`to_dict` produces the on-disk shape.
    """

    benchmark: str
    run_id: str
    started_at: str
    completed_at: str
    config: dict[str, Any]
    summary: dict[str, Any]
    questions: list[QuestionResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "config": dict(self.config),
            "summary": dict(self.summary),
            "questions": [
                {
                    "question_id": q.question_id,
                    "conversation_id": q.conversation_id,
                    "category": q.category,
                    "expected_evidence": list(q.expected_evidence),
                    "retrieved_ids": list(q.retrieved_ids),
                    "retrieved_names": list(q.retrieved_names),
                    "score": q.score,
                    "matched": list(q.matched),
                    "missed": list(q.missed),
                    "latency_ms": q.latency_ms,
                }
                for q in self.questions
            ],
        }

    def write_json(self, path: str | Path) -> Path:
        """Serialise ``self`` to ``path`` and return the resolved path."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class BenchmarkRunner:
    """Replay a benchmark dataset into engrama and score each question.

    Parameters:
        benchmark: A *loaded* :class:`Benchmark` instance.
        scorer: Strategy that grades each retrieval (default
            :class:`~engrama.bench.scoring.RecallAtK` with ``k=5``).
        retrieval_limit: How many search hits to pull per question
            before handing the list to the scorer. Defaults to 10 so a
            ``recall@K`` scorer with ``K<=10`` always sees the full
            window.
        db_root: Optional directory under which per-cycle SQLite files
            are created. Defaults to a temp directory cleaned up on
            :meth:`run` completion.
        engrama_version: Recorded in the report for reproducibility.
    """

    def __init__(
        self,
        benchmark: Benchmark,
        *,
        scorer: Scorer | None = None,
        retrieval_limit: int = _DEFAULT_RETRIEVAL_LIMIT,
        db_root: str | Path | None = None,
        engrama_version: str | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.scorer = scorer or RecallAtK(k=5)
        self.retrieval_limit = retrieval_limit
        # ``.resolve()`` collapses any ``..`` traversal so a misuse
        # (intentional or otherwise) of ``--db-root ../../tmp`` can't
        # silently mkdir outside the operator's intended tree.
        self.db_root = Path(db_root).resolve() if db_root is not None else None
        self.engrama_version = engrama_version or self._lookup_engrama_version()
        self.run_id = f"bench-{benchmark.name}-{uuid.uuid4().hex[:8]}"
        # Replay-time failure counter — incremented by ``_replay_conversation``
        # whenever a ``remember()`` call raises so the run summary can
        # surface partial-ingest runs instead of pretending a corrupted
        # replay produced a clean baseline.
        self._failed_turns: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, *, limit: int | None = None) -> BenchmarkReport:
        """Replay the dataset and produce a :class:`BenchmarkReport`.

        ``limit``, if set, caps the number of questions actually scored
        — useful for smoke runs against the full LOCOMO without burning
        ~30 minutes on a laptop.
        """
        started = datetime.now(UTC)

        results: list[QuestionResult] = list(self._iter_results(limit=limit))

        completed = datetime.now(UTC)
        summary = self._summarise(results, started, completed)
        return BenchmarkReport(
            benchmark=self.benchmark.name,
            run_id=self.run_id,
            started_at=started.isoformat(),
            completed_at=completed.isoformat(),
            config={
                "limit": limit,
                "scorer": self.scorer.metric,
                "retrieval_limit": self.retrieval_limit,
                "lifecycle": self.benchmark.lifecycle,
                "engrama_version": self.engrama_version,
            },
            summary=summary,
            questions=results,
        )

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def _iter_results(self, *, limit: int | None) -> Iterator[QuestionResult]:
        scored = 0
        with self._tempdir() as tmp:
            for convo in self.benchmark.iter_conversations():
                if limit is not None and scored >= limit:
                    return
                questions = list(self._questions_for(convo))
                if not questions:
                    continue
                if self.benchmark.lifecycle == "per-conversation":
                    for q in self._run_per_conversation(convo, questions, tmp):
                        yield q
                        scored += 1
                        if limit is not None and scored >= limit:
                            return
                elif self.benchmark.lifecycle == "per-question":
                    for q in self._run_per_question(convo, questions, tmp):
                        yield q
                        scored += 1
                        if limit is not None and scored >= limit:
                            return
                else:  # pragma: no cover - defensive
                    raise ValueError(
                        f"Unknown lifecycle {self.benchmark.lifecycle!r} on "
                        f"{type(self.benchmark).__name__}"
                    )

    def _run_per_conversation(
        self,
        convo: BenchmarkConversation,
        questions: list[BenchmarkQuestion],
        tmp: Path,
    ) -> Iterator[QuestionResult]:
        db_path = tmp / f"{_safe(convo.conversation_id)}.db"
        with self._open_engrama(db_path) as eng:
            self._replay_conversation(eng, convo)
            for q in questions:
                yield self._answer_question(eng, q)

    def _run_per_question(
        self,
        convo: BenchmarkConversation,
        questions: list[BenchmarkQuestion],
        tmp: Path,
    ) -> Iterator[QuestionResult]:
        # LongMemEval gives each question its own self-contained
        # haystack, but we group by conversation id (== question id in
        # the loader). One DB per question keeps the contract clean.
        for q in questions:
            db_path = tmp / f"{_safe(q.question_id)}.db"
            with self._open_engrama(db_path) as eng:
                self._replay_conversation(eng, convo)
                yield self._answer_question(eng, q)

    def _questions_for(self, convo: BenchmarkConversation) -> Iterator[BenchmarkQuestion]:
        for q in self.benchmark.iter_questions():
            if q.conversation_id == convo.conversation_id:
                yield q

    # ------------------------------------------------------------------
    # Replay + query
    # ------------------------------------------------------------------

    def _replay_conversation(self, eng: Engrama, convo: BenchmarkConversation) -> None:
        """Write every turn of ``convo`` into engrama via ``remember``.

        Each turn becomes one :class:`Concept` node:

        * ``name`` is a stable identifier the recall scorer can match
          against the dataset's evidence pointer (see :meth:`_turn_name`).
        * ``valid_from`` is the session's date, when present, so engrama
          temporal scoring has a real timestamp to work with.
        """
        session_dates = _session_dates_for(convo)
        for session_idx, session in enumerate(convo.sessions):
            session_date = session_dates.get(session_idx)
            for turn_idx, turn in enumerate(session):
                name = self._turn_name(convo, session_idx, turn_idx, turn)
                observation = self._format_observation(turn)
                extra: dict[str, Any] = {}
                if session_date is not None:
                    extra["valid_from"] = session_date
                # Surface the speaker on the node so a future scorer (or
                # someone reading the DB) can tell who said what.
                if turn.speaker:
                    extra["speaker"] = turn.speaker
                try:
                    eng.remember(_TURN_LABEL, name, observation, **extra)
                except Exception:
                    self._failed_turns += 1
                    logger.exception(
                        "remember failed for %s session_idx=%d turn_idx=%d",
                        convo.conversation_id,
                        session_idx,
                        turn_idx,
                    )

    def _answer_question(self, eng: Engrama, question: BenchmarkQuestion) -> QuestionResult:
        start = time.perf_counter()
        hits: list[dict[str, Any]] = []
        # FTS5's default MATCH grammar treats whitespace as AND, so a
        # natural-language question rarely matches any single turn that
        # is shorter than the question itself. For benchmark retrieval
        # we want OR semantics: relevance ranking then surfaces the best
        # partial match. Rewriting here keeps the engine's behaviour
        # unchanged for production callers.
        query = _or_join_tokens(question.question)
        try:
            hits = eng.search(query, limit=self.retrieval_limit)
        except Exception:
            logger.exception("search failed for question %s", question.question_id)
        latency_ms = (time.perf_counter() - start) * 1000.0

        retrieved_names = [str(h.get("name", "")) for h in hits if h.get("name")]
        retrieved_ids = [
            f"{h.get('type', '')}:{h.get('name', '')}".strip(":") for h in hits if h.get("name")
        ]
        report: ScoreReport = self.scorer.score(
            RetrievalRun(
                question_id=question.question_id,
                expected_evidence=list(question.evidence),
                retrieved_ids=retrieved_ids,
                retrieved_names=retrieved_names,
                answer_text=None,
            )
        )
        return QuestionResult(
            question_id=question.question_id,
            conversation_id=question.conversation_id,
            category=question.category,
            expected_evidence=list(question.evidence),
            retrieved_ids=retrieved_ids,
            retrieved_names=retrieved_names,
            score=report.score,
            matched=list(report.matched),
            missed=list(report.missed),
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _summarise(
        self,
        results: list[QuestionResult],
        started: datetime,
        completed: datetime,
    ) -> dict[str, Any]:
        if not results:
            return {
                "questions_total": 0,
                "questions_scored": 0,
                "questions_with_evidence": 0,
                "mean_score": 0.0,
                "mean_latency_ms": 0.0,
                "duration_seconds": (completed - started).total_seconds(),
                "failed_turns": self._failed_turns,
            }
        with_evidence = sum(1 for q in results if q.expected_evidence)
        mean_score = sum(q.score for q in results) / len(results)
        mean_latency = sum(q.latency_ms for q in results) / len(results)
        return {
            "questions_total": len(results),
            "questions_scored": len(results),
            "questions_with_evidence": with_evidence,
            "mean_score": round(mean_score, 4),
            "mean_latency_ms": round(mean_latency, 2),
            "duration_seconds": round((completed - started).total_seconds(), 2),
            # Non-zero ``failed_turns`` means the replay loop swallowed
            # an exception while ingesting one or more turns — the
            # numbers above are still meaningful but the benchmark DB
            # is incomplete.
            "failed_turns": self._failed_turns,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _open_engrama(self, db_path: Path) -> Engrama:
        from engrama.adapters.sdk import Engrama

        return Engrama(
            backend="sqlite",
            db_path=db_path,
            source_agent="bench",
            source_session=self.run_id,
            session_id=self.run_id,
        )

    def _tempdir(self) -> Any:
        if self.db_root is not None:
            self.db_root.mkdir(parents=True, exist_ok=True)
            return _PathContext(self.db_root)
        return _TempDirContext()

    @staticmethod
    def _format_observation(turn: BenchmarkTurn) -> str:
        if turn.speaker:
            return f"{turn.speaker}: {turn.text}"
        return turn.text

    @staticmethod
    def _turn_name(
        convo: BenchmarkConversation,
        session_idx: int,
        turn_idx: int,
        turn: BenchmarkTurn,
    ) -> str:
        """Stable identifier the recall scorer can match against evidence.

        Order of preference:

        1. Turn-level ``dia_id`` metadata (LOCOMO) — already matches the
           dataset's evidence pointer verbatim (e.g. ``"D1:1"``).
        2. ``session_id`` metadata + turn index (LongMemEval) — the
           recall scorer prefix-matches the session id against the
           dataset's session-level evidence.
        3. Fallback: ``session_<idx>:turn_<idx>`` so two turns never
           collide on a generic file with no metadata.
        """
        dia_id = turn.metadata.get("dia_id")
        if isinstance(dia_id, str) and dia_id:
            return dia_id
        session_id = turn.metadata.get("session_id")
        if isinstance(session_id, str) and session_id:
            return f"{session_id}:{turn_idx}"
        return f"{convo.conversation_id}:s{session_idx}:t{turn_idx}"

    @staticmethod
    def _lookup_engrama_version() -> str:
        try:
            from engrama import __version__

            return str(__version__)
        except Exception:  # pragma: no cover - defensive
            return "unknown"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _safe(value: str) -> str:
    """Render ``value`` safe to use as a filename component."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value) or "_"


# Words that carry no retrieval signal and only add noise when OR-joined.
# Deliberately small: aggressively trimming stop-words risks dropping the
# one content word that disambiguates a question.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "or",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "of",
        "in",
        "on",
        "at",
        "to",
        "from",
        "for",
        "with",
        "by",
        "as",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "them",
        "his",
        "her",
        "their",
        "our",
        "my",
        "your",
        "what",
        "when",
        "where",
        "who",
        "why",
        "how",
    }
)

# Reserved FTS5 keywords that would change the grammar if they appeared
# bare in the OR-joined query. The engine sanitiser also quotes any token
# that's not pure ASCII alnum, so anything in this list is escaped to a
# quoted phrase here to be unambiguous.
_FTS5_OPERATORS: frozenset[str] = frozenset({"AND", "OR", "NOT", "NEAR"})


def _or_join_tokens(query: str) -> str:
    """Turn a natural-language question into an OR-joined FTS5 query.

    Strips punctuation, drops stop words, and emits ``a OR b OR c`` so
    FTS5's relevance ranking decides which turn is the best partial
    match — instead of demanding every token appear in the same node.
    """
    raw = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in query)
    tokens: list[str] = []
    seen: set[str] = set()
    for tok in raw.split():
        upper = tok.upper()
        is_op = upper in _FTS5_OPERATORS
        low = tok.lower()
        # FTS5 operator keywords win over stop-word stripping: the user
        # may legitimately ask about the word ``or``, in which case we
        # must quote it rather than drop it.
        if not is_op and low in _STOP_WORDS:
            continue
        if low in seen:
            continue
        seen.add(low)
        if is_op:
            tokens.append(f'"{tok}"')
        else:
            tokens.append(tok)
    if not tokens:
        return query.strip()
    return " OR ".join(tokens)


def _session_dates_for(convo: BenchmarkConversation) -> dict[int, str]:
    """Map session index → ISO date string, when the dataset ships one.

    Supports both LOCOMO's ``metadata['session_dates']`` (keyed by
    ``session_<N>_date_time``) and LongMemEval's
    ``metadata['haystack_dates']`` (positional list).
    """
    out: dict[int, str] = {}

    # LOCOMO
    raw = convo.metadata.get("session_dates")
    if isinstance(raw, dict):
        for key, value in raw.items():
            # LOCOMO uses 1-based session numbering — convert to the
            # 0-based session list index used elsewhere in the runner.
            if not isinstance(value, str):
                continue
            parts = key.split("_")
            if len(parts) >= 2 and parts[0] == "session" and parts[1].isdigit():
                idx = int(parts[1]) - 1
                # ``session_0_date_time`` would yield idx=-1 which is
                # silently wrap-around-valid on a Python list. Drop
                # such entries instead of poisoning the session map.
                if idx < 0:
                    continue
                iso = _to_iso_date(value)
                if iso:
                    out[idx] = iso

    # LongMemEval
    dates = convo.metadata.get("haystack_dates")
    if isinstance(dates, list):
        for idx, value in enumerate(dates):
            if isinstance(value, str):
                iso = _to_iso_date(value)
                if iso:
                    out.setdefault(idx, iso)
    return out


def _to_iso_date(value: str) -> str | None:
    """Best-effort coercion to an ISO-8601 timestamp engrama can store.

    Accepts ``YYYY-MM-DD`` and ``YYYY-MM-DD HH:MM`` (LOCOMO's format).
    Returns ``None`` for shapes we don't recognise so the caller can
    fall back to no timestamp rather than persisting garbage.
    """
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=UTC)
            return dt.isoformat()
        except ValueError:
            continue
    return None


class _TempDirContext:
    """Thin context manager wrapping :class:`tempfile.TemporaryDirectory`.

    Returns the directory as a :class:`Path` instead of a string and
    suppresses the cleanup race that occurs on Windows when a SQLite
    connection has just been closed — the GC may still hold the file
    momentarily, and the OS deletion errors with ``WinError 32``.
    """

    def __enter__(self) -> Path:
        self._td = tempfile.TemporaryDirectory(prefix="engrama-bench-")
        return Path(self._td.name)

    def __exit__(self, *exc: Any) -> None:
        try:
            self._td.cleanup()
        except PermissionError:
            logger.debug("Temp dir cleanup skipped — Windows file lock")


class _PathContext:
    """Context manager wrapping a caller-supplied directory.

    Identical interface to :class:`_TempDirContext` but does not delete
    on exit — the caller asked for a persistent location.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> Path:
        return self._path

    def __exit__(self, *exc: Any) -> None:
        pass


def run_benchmark(
    benchmark: Benchmark,
    *,
    scorer: str | Scorer = "recall@5",
    limit: int | None = None,
    retrieval_limit: int = _DEFAULT_RETRIEVAL_LIMIT,
    db_root: str | Path | None = None,
) -> BenchmarkReport:
    """Convenience: run ``benchmark`` and return the report.

    Wraps :class:`BenchmarkRunner` so the CLI handler stays short.
    """
    resolved: Scorer
    if isinstance(scorer, str):
        resolved = build_scorer(scorer)
    else:
        resolved = scorer
    runner = BenchmarkRunner(
        benchmark,
        scorer=resolved,
        retrieval_limit=retrieval_limit,
        db_root=db_root,
    )
    return runner.run(limit=limit)


__all__ = [
    "BenchmarkRunner",
    "BenchmarkReport",
    "QuestionResult",
    "run_benchmark",
]
