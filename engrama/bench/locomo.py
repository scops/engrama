"""LOCOMO benchmark loader (Roadmap P15, dataset 1/2).

Source: https://github.com/snap-stanford/locomo

The public release ships a single JSON file (``locomo10.json``) whose
top-level value is a list of *samples*. Each sample bundles one
multi-session conversation with a separate ``qa`` list of questions
asked over that conversation::

    [
      {
        "sample_id": "0",
        "conversation": {
          "speaker_a": "Caroline",
          "speaker_b": "Melanie",
          "session_1_date_time": "...",
          "session_1": [
            {"speaker": "Caroline", "text": "...", "dia_id": "D1:1"},
            ...
          ],
          "session_2_date_time": "...",
          "session_2": [...],
          ...
        },
        "qa": [
          {
            "question": "When did Caroline visit ...?",
            "answer": "On 5 May 2023.",
            "evidence": ["D5:12"],
            "category": 1
          },
          ...
        ]
      },
      ...
    ]

The loader is tolerant to two practical shapes:

* the full list-of-samples file shipped on GitHub
* a single sample dict (handy for tests and pulling one conversation
  in isolation for local debugging)
"""

from __future__ import annotations

import re
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

_SESSION_KEY = re.compile(r"^session_(\d+)$")


def _session_keys_in_order(conversation: dict[str, Any]) -> list[str]:
    """Return ``session_1``, ``session_2``, ... in ascending session order.

    The conversation dict also contains ``session_N_date_time`` siblings
    plus speaker names — we ignore those for the transcript itself but
    surface them in conversation/turn metadata where useful.
    """
    indexed: list[tuple[int, str]] = []
    for key in conversation.keys():
        m = _SESSION_KEY.match(key)
        if m:
            indexed.append((int(m.group(1)), key))
    indexed.sort()
    return [key for _, key in indexed]


class LocomoBenchmark(Benchmark):
    """LOCOMO long-conversation memory benchmark."""

    name = "locomo"

    def load(self, path: str | Path) -> None:
        data = _read_json(path)
        if isinstance(data, dict):
            # Single-sample shape — wrap so iter_* logic stays uniform.
            data = [data]
        if not isinstance(data, list):
            raise ValueError(
                f"LOCOMO source must be a list (or single sample dict); got {type(data).__name__}"
            )
        self._raw = data
        self._path = Path(path)

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def iter_conversations(self) -> Iterator[BenchmarkConversation]:
        for sample in self._samples():
            yield self._to_conversation(sample)

    def iter_questions(self) -> Iterator[BenchmarkQuestion]:
        for sample in self._samples():
            convo_id = self._sample_id(sample)
            for index, qa in enumerate(sample.get("qa") or []):
                yield self._to_question(convo_id, index, qa)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _samples(self) -> list[dict[str, Any]]:
        if self._raw is None:
            raise RuntimeError("LocomoBenchmark.load() must be called before iteration")
        return self._raw  # type: ignore[return-value]

    @staticmethod
    def _sample_id(sample: dict[str, Any]) -> str:
        # snap-stanford uses ``sample_id``; some forks use ``id``. Prefer
        # ``sample_id`` then ``id``, fall back to a stable enumeration
        # marker so missing-id files still produce deterministic question
        # ids on subsequent runs.
        return str(sample.get("sample_id") or sample.get("id") or "?")

    def _to_conversation(self, sample: dict[str, Any]) -> BenchmarkConversation:
        convo = sample.get("conversation") or {}
        sessions: list[list[BenchmarkTurn]] = []
        for key in _session_keys_in_order(convo):
            turns = []
            for turn in convo[key] or []:
                turn_meta: dict[str, Any] = {}
                if "dia_id" in turn:
                    turn_meta["dia_id"] = turn["dia_id"]
                if "img_url" in turn:
                    turn_meta["img_url"] = turn["img_url"]
                turns.append(
                    BenchmarkTurn(
                        speaker=turn.get("speaker", ""),
                        text=turn.get("text", ""),
                        metadata=turn_meta,
                    )
                )
            sessions.append(turns)

        meta: dict[str, Any] = {}
        for k in ("speaker_a", "speaker_b"):
            if k in convo:
                meta[k] = convo[k]
        # Surface every ``session_N_date_time`` we saw so the runner can
        # inject events with the right timestamp later (PR-G3).
        dates: dict[str, str] = {}
        for key, value in convo.items():
            if key.endswith("_date_time"):
                dates[key] = value
        if dates:
            meta["session_dates"] = dates

        return BenchmarkConversation(
            conversation_id=self._sample_id(sample),
            sessions=sessions,
            metadata=meta,
        )

    @staticmethod
    def _to_question(convo_id: str, index: int, qa: dict[str, Any]) -> BenchmarkQuestion:
        # LOCOMO has both ``answer`` and (sometimes) ``adversarial_answer``;
        # the runner / scorer can pick the policy. Here we expose the
        # canonical answer as ``expected_answer`` and stash the rest in
        # metadata so nothing is lost on the way through.
        meta: dict[str, Any] = {}
        if "adversarial_answer" in qa:
            meta["adversarial_answer"] = qa["adversarial_answer"]
        category = qa.get("category")
        category_str = str(category) if category is not None else None
        evidence = list(qa.get("evidence") or [])
        return BenchmarkQuestion(
            question_id=f"{convo_id}:q{index}",
            conversation_id=convo_id,
            question=str(qa.get("question", "")),
            expected_answer=str(qa.get("answer", "")),
            category=category_str,
            evidence=evidence,
            metadata=meta,
        )


__all__ = ["LocomoBenchmark"]
