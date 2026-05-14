"""LongMemEval benchmark loader (Roadmap P15, dataset 2/2).

Source: https://github.com/xiaowu0162/LongMemEval

LongMemEval ships a single JSON file whose top-level value is a list of
*question records*. Each record bundles one question together with the
exact context (haystack) the model is allowed to use to answer it::

    [
      {
        "question_id": "qsn_0001",
        "question_type": "single-session-user",
        "question": "What did the user say about X?",
        "answer": "They said Y.",
        "haystack_session_ids": ["s1", "s2", ...],
        "haystack_dates":       ["2024-01-01", "2024-01-04", ...],
        "haystack_sessions": [
          [
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."}
          ],
          [...]
        ],
        "answer_session_ids": ["s5"]
      },
      ...
    ]

There is **no shared conversation** across questions — every record
carries its own haystack. We therefore map each record to its own
:class:`BenchmarkConversation` (``conversation_id = question_id``) plus
the matching :class:`BenchmarkQuestion`. Session ids and dates ride
along on conversation metadata so the runner (PR-G3) can position
turns in time when replaying them into engrama.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from engrama.bench.core import (
    Benchmark,
    BenchmarkConversation,
    BenchmarkQuestion,
    BenchmarkTurn,
    _read_json,
)


class LongMemEvalBenchmark(Benchmark):
    """LongMemEval long-term memory evaluation benchmark."""

    name = "longmemeval"

    def load(self, path: str | Path) -> None:
        data = _read_json(path)
        if isinstance(data, dict):
            # Single-record shape — wrap so iter_* logic stays uniform.
            data = [data]
        if not isinstance(data, list):
            raise ValueError(
                f"LongMemEval source must be a list (or single record dict); "
                f"got {type(data).__name__}"
            )
        self._raw = data
        self._path = Path(path)

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def iter_conversations(self) -> Iterator[BenchmarkConversation]:
        for record in self._records():
            yield self._to_conversation(record)

    def iter_questions(self) -> Iterator[BenchmarkQuestion]:
        for record in self._records():
            yield self._to_question(record)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _records(self) -> list[dict[str, Any]]:
        if self._raw is None:
            raise RuntimeError("LongMemEvalBenchmark.load() must be called before iteration")
        return self._raw  # type: ignore[return-value]

    @staticmethod
    def _question_id(record: dict[str, Any]) -> str:
        # Records always carry ``question_id``; fall back to ``id`` for
        # any fork that renames it, and to ``"?"`` only as last resort
        # so iteration stays deterministic on malformed files.
        return str(record.get("question_id") or record.get("id") or "?")

    def _to_conversation(self, record: dict[str, Any]) -> BenchmarkConversation:
        convo_id = self._question_id(record)
        haystack_sessions = record.get("haystack_sessions") or []
        session_ids = record.get("haystack_session_ids") or []
        dates = record.get("haystack_dates") or []

        sessions: list[list[BenchmarkTurn]] = []
        for index, session in enumerate(haystack_sessions):
            turns: list[BenchmarkTurn] = []
            session_meta: dict[str, Any] = {}
            if index < len(session_ids):
                session_meta["session_id"] = session_ids[index]
            if index < len(dates):
                session_meta["date"] = dates[index]
            for turn in session or []:
                # LongMemEval uses {role, content}; LOCOMO uses
                # {speaker, text}. Normalise onto the common shape so
                # downstream code never has to peek at the source.
                turn_meta = dict(session_meta)
                if "has_answer" in turn:
                    turn_meta["has_answer"] = turn["has_answer"]
                turns.append(
                    BenchmarkTurn(
                        speaker=str(turn.get("role", "")),
                        text=str(turn.get("content", "")),
                        metadata=turn_meta,
                    )
                )
            sessions.append(turns)

        meta: dict[str, Any] = {}
        if session_ids:
            meta["haystack_session_ids"] = list(session_ids)
        if dates:
            meta["haystack_dates"] = list(dates)

        return BenchmarkConversation(
            conversation_id=convo_id,
            sessions=sessions,
            metadata=meta,
        )

    def _to_question(self, record: dict[str, Any]) -> BenchmarkQuestion:
        convo_id = self._question_id(record)
        category = record.get("question_type")
        category_str = str(category) if category is not None else None
        # LongMemEval calls the evidence pointer ``answer_session_ids``;
        # we keep the native value list so a scorer can match it against
        # the session-id metadata on each turn.
        evidence = [str(s) for s in record.get("answer_session_ids") or []]
        return BenchmarkQuestion(
            question_id=convo_id,
            conversation_id=convo_id,
            question=str(record.get("question", "")),
            expected_answer=str(record.get("answer", "")),
            category=category_str,
            evidence=evidence,
        )


__all__ = ["LongMemEvalBenchmark"]
