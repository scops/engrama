"""Tests for the benchmark scaffold (Roadmap P15 / DDR-003 Part 7).

PR-G1 ships loaders + a `bench list` CLI subcommand only — no runner,
no scoring, no engrama interaction. These tests cover that surface:
the core dataclasses, the LOCOMO parser against a tiny fixture, and
the CLI's count / preview behaviour.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from engrama.bench import (
    Benchmark,
    BenchmarkConversation,
    BenchmarkQuestion,
    LocomoBenchmark,
    LongMemEvalBenchmark,
)
from engrama.bench.core import BenchmarkTurn

LOCOMO_FIXTURE = Path(__file__).parent / "data" / "locomo_mini.json"
LONGMEMEVAL_FIXTURE = Path(__file__).parent / "data" / "longmemeval_mini.json"


# ---------------------------------------------------------------------------
# 1. Core dataclasses
# ---------------------------------------------------------------------------


class TestCoreDataclasses:
    def test_turn_defaults(self):
        t = BenchmarkTurn(speaker="alice", text="hi")
        assert t.speaker == "alice"
        assert t.text == "hi"
        assert t.metadata == {}

    def test_conversation_defaults(self):
        c = BenchmarkConversation(conversation_id="c0", sessions=[])
        assert c.conversation_id == "c0"
        assert c.sessions == []
        assert c.metadata == {}

    def test_question_defaults(self):
        q = BenchmarkQuestion(
            question_id="c0:q0",
            conversation_id="c0",
            question="why?",
            expected_answer="because",
        )
        assert q.category is None
        assert q.evidence == []
        assert q.metadata == {}

    def test_benchmark_is_abstract(self):
        # Can't instantiate the ABC directly — guarantees subclasses
        # implement the load + iter_* contract.
        with pytest.raises(TypeError):
            Benchmark()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# 2. LOCOMO loader
# ---------------------------------------------------------------------------


@pytest.fixture()
def loaded_locomo() -> LocomoBenchmark:
    bench = LocomoBenchmark()
    bench.load(LOCOMO_FIXTURE)
    return bench


class TestLocomoLoader:
    def test_iteration_before_load_raises(self):
        bench = LocomoBenchmark()
        with pytest.raises(RuntimeError, match="load"):
            list(bench.iter_questions())

    def test_counts(self, loaded_locomo):
        assert loaded_locomo.conversation_count() == 2
        assert loaded_locomo.question_count() == 3

    def test_conversation_shape(self, loaded_locomo):
        convos = list(loaded_locomo.iter_conversations())
        first = convos[0]
        assert first.conversation_id == "mini-0"
        # Two sessions, in order, with the right turn counts.
        assert len(first.sessions) == 2
        assert len(first.sessions[0]) == 2
        assert len(first.sessions[1]) == 1
        # Session-date-time fields are surfaced in metadata.
        assert "session_dates" in first.metadata
        assert first.metadata["session_dates"]["session_1_date_time"] == "2023-05-05 09:00"
        # Speaker names propagate.
        assert first.metadata["speaker_a"] == "Caroline"
        # First turn carries the dia_id reference.
        assert first.sessions[0][0].speaker == "Caroline"
        assert first.sessions[0][0].metadata["dia_id"] == "D1:1"

    def test_question_shape(self, loaded_locomo):
        questions = list(loaded_locomo.iter_questions())
        assert len(questions) == 3
        # IDs are derived from conversation id + question index.
        ids = [q.question_id for q in questions]
        assert ids == ["mini-0:q0", "mini-0:q1", "mini-1:q0"]
        # Evidence list is preserved verbatim.
        assert questions[0].evidence == ["D1:1"]
        # Category becomes a string for uniform downstream handling.
        assert questions[0].category == "1"
        # Adversarial answers ride along in metadata (not in expected_answer).
        assert questions[1].metadata["adversarial_answer"].startswith("No")
        # Questions without a category come back with category=None.
        assert questions[2].category is None

    def test_single_sample_dict_is_accepted(self, tmp_path):
        # A loose single-sample dict (no list wrap) should also load —
        # useful for ad-hoc debugging on one conversation at a time.
        single = {
            "sample_id": "solo",
            "conversation": {"session_1": [{"speaker": "x", "text": "hi"}]},
            "qa": [{"question": "?", "answer": "."}],
        }
        path = tmp_path / "solo.json"
        path.write_text(json.dumps(single), encoding="utf-8")
        bench = LocomoBenchmark()
        bench.load(path)
        assert bench.conversation_count() == 1
        assert bench.question_count() == 1

    def test_non_list_non_dict_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('"just a string"', encoding="utf-8")
        bench = LocomoBenchmark()
        with pytest.raises(ValueError, match="list"):
            bench.load(path)


# ---------------------------------------------------------------------------
# 3. LongMemEval loader
# ---------------------------------------------------------------------------


@pytest.fixture()
def loaded_longmemeval() -> LongMemEvalBenchmark:
    bench = LongMemEvalBenchmark()
    bench.load(LONGMEMEVAL_FIXTURE)
    return bench


class TestLongMemEvalLoader:
    def test_iteration_before_load_raises(self):
        bench = LongMemEvalBenchmark()
        with pytest.raises(RuntimeError, match="load"):
            list(bench.iter_questions())

    def test_counts(self, loaded_longmemeval):
        # LongMemEval is one question per record, so conversations == questions.
        assert loaded_longmemeval.conversation_count() == 3
        assert loaded_longmemeval.question_count() == 3

    def test_question_shape(self, loaded_longmemeval):
        questions = list(loaded_longmemeval.iter_questions())
        ids = [q.question_id for q in questions]
        assert ids == ["qsn_0001", "qsn_0002", "qsn_0003"]
        # question_type → category, stringified.
        assert questions[0].category == "single-session-user"
        assert questions[1].category == "multi-session"
        # Missing question_type comes back as None.
        assert questions[2].category is None
        # answer_session_ids → evidence list (strings).
        assert questions[0].evidence == ["s2"]
        # Record without answer_session_ids → empty evidence.
        assert questions[2].evidence == []

    def test_conversation_shape(self, loaded_longmemeval):
        convos = list(loaded_longmemeval.iter_conversations())
        first = convos[0]
        # Each record produces its own conversation, ID == question_id.
        assert first.conversation_id == "qsn_0001"
        # Two sessions with 2 + 1 turns.
        assert len(first.sessions) == 2
        assert len(first.sessions[0]) == 2
        assert len(first.sessions[1]) == 1
        # role → speaker, content → text, with session metadata on each turn.
        first_turn = first.sessions[0][0]
        assert first_turn.speaker == "user"
        assert first_turn.text.startswith("I just bought")
        assert first_turn.metadata["session_id"] == "s1"
        assert first_turn.metadata["date"] == "2024-05-04"
        # has_answer rides through if present.
        assert first.sessions[1][0].metadata["has_answer"] is True
        # Conversation metadata surfaces the raw id+date arrays.
        assert first.metadata["haystack_session_ids"] == ["s1", "s2"]
        assert first.metadata["haystack_dates"] == ["2024-05-04", "2024-05-12"]

    def test_empty_haystack_is_supported(self, loaded_longmemeval):
        third = list(loaded_longmemeval.iter_conversations())[2]
        # Empty haystack_sessions → zero sessions, no leftover metadata.
        assert third.sessions == []
        assert "haystack_session_ids" not in third.metadata

    def test_single_record_dict_is_accepted(self, tmp_path):
        single = {
            "question_id": "solo",
            "question": "?",
            "answer": ".",
            "haystack_sessions": [],
        }
        path = tmp_path / "solo.json"
        path.write_text(json.dumps(single), encoding="utf-8")
        bench = LongMemEvalBenchmark()
        bench.load(path)
        assert bench.question_count() == 1

    def test_non_list_non_dict_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('"just a string"', encoding="utf-8")
        bench = LongMemEvalBenchmark()
        with pytest.raises(ValueError, match="list"):
            bench.load(path)


# ---------------------------------------------------------------------------
# 4. CLI: `engrama bench list`
# ---------------------------------------------------------------------------


def _run_engrama_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m engrama.cli`` so the test doesn't need the
    installed ``engrama`` console script (works in clean checkouts)."""
    return subprocess.run(
        [sys.executable, "-m", "engrama.cli", *args],
        check=False,
        capture_output=True,
        text=True,
    )


class TestBenchListCli:
    def test_list_prints_counts_and_preview(self):
        proc = _run_engrama_cli(
            "bench",
            "list",
            "--benchmark",
            "locomo",
            "--data-path",
            str(LOCOMO_FIXTURE),
        )
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout
        assert "benchmark: locomo" in out
        assert "conversations: 2" in out
        assert "questions: 3" in out
        # Preview includes question text from the fixture.
        assert "When did Caroline first visit Lisbon?" in out

    def test_list_respects_zero_limit(self):
        proc = _run_engrama_cli(
            "bench",
            "list",
            "--benchmark",
            "locomo",
            "--data-path",
            str(LOCOMO_FIXTURE),
            "--limit",
            "0",
        )
        assert proc.returncode == 0
        # Counts still print, preview block is suppressed.
        assert "questions: 3" in proc.stdout
        assert "first" not in proc.stdout

    def test_list_unknown_benchmark_errors(self):
        proc = _run_engrama_cli(
            "bench",
            "list",
            "--benchmark",
            "does-not-exist",
            "--data-path",
            str(LOCOMO_FIXTURE),
        )
        # argparse's `choices=` rejects this before our code runs, so
        # the return code is 2 (argparse's usage error) and the error
        # lands on stderr.
        assert proc.returncode != 0
        assert "does-not-exist" in proc.stderr

    def test_list_missing_subcommand_errors(self):
        proc = _run_engrama_cli("bench")
        assert proc.returncode != 0
        assert "bench" in proc.stderr.lower() or "bench" in proc.stdout.lower()

    def test_list_longmemeval(self):
        proc = _run_engrama_cli(
            "bench",
            "list",
            "--benchmark",
            "longmemeval",
            "--data-path",
            str(LONGMEMEVAL_FIXTURE),
        )
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout
        assert "benchmark: longmemeval" in out
        assert "conversations: 3" in out
        assert "questions: 3" in out
        assert "What hobby did the user take up in May?" in out
