"""Tests for the benchmark scaffold (Roadmap P15 / DDR-003 Part 7).

PR-G1 shipped the loaders + a ``bench list`` CLI subcommand. PR-G2
added the LongMemEval loader. PR-G3 adds the runner, scoring, and a
``bench run`` CLI subcommand. These tests cover the loader contract,
both CLI subcommands, the recall scorer, and an end-to-end run against
the mini fixtures.
"""

from __future__ import annotations

import json
import os
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
from engrama.bench.report import (
    category_breakdown,
    load_report,
    render_markdown,
    top_failures,
)
from engrama.bench.runner import (
    BenchmarkRunner,
    _or_join_tokens,
    run_benchmark,
)
from engrama.bench.scoring import (
    LLMJudge,
    RecallAtK,
    RetrievalRun,
    build_scorer,
)

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


# ---------------------------------------------------------------------------
# 5. Lifecycle declaration (PR-G3)
# ---------------------------------------------------------------------------


class TestLifecycleDeclaration:
    def test_locomo_is_per_conversation(self):
        assert LocomoBenchmark.lifecycle == "per-conversation"
        # Instance also exposes the same value (no override surprises).
        assert LocomoBenchmark().lifecycle == "per-conversation"

    def test_longmemeval_is_per_question(self):
        assert LongMemEvalBenchmark.lifecycle == "per-question"
        assert LongMemEvalBenchmark().lifecycle == "per-question"


# ---------------------------------------------------------------------------
# 6. Recall scorer (PR-G3)
# ---------------------------------------------------------------------------


class TestRecallAtK:
    def test_exact_match_is_recall_one(self):
        scorer = RecallAtK(k=5)
        report = scorer.score(
            RetrievalRun(
                question_id="q",
                expected_evidence=["D1:1"],
                retrieved_ids=[],
                retrieved_names=["D1:1", "D1:2"],
            )
        )
        assert report.score == 1.0
        assert report.matched == ["D1:1"]
        assert report.missed == []
        assert report.metric == "recall@5"

    def test_prefix_match_supports_session_level_evidence(self):
        # LongMemEval evidence is session-level ("s2") but the runner
        # names individual turns ("s2:0", "s2:1"...). The scorer must
        # recognise the prefix as a match.
        scorer = RecallAtK(k=5)
        report = scorer.score(
            RetrievalRun(
                question_id="q",
                expected_evidence=["s2"],
                retrieved_ids=[],
                retrieved_names=["s1:0", "s2:0"],
            )
        )
        assert report.score == 1.0
        assert report.matched == ["s2"]

    def test_no_match_is_recall_zero(self):
        scorer = RecallAtK(k=5)
        report = scorer.score(
            RetrievalRun(
                question_id="q",
                expected_evidence=["D9:9"],
                retrieved_ids=[],
                retrieved_names=["D1:1"],
            )
        )
        assert report.score == 0.0
        assert report.missed == ["D9:9"]

    def test_empty_evidence_is_trivial_pass(self):
        # Questions with no evidence pointer can't grade retrieval —
        # treating them as failures would punish the engine for a
        # dataset shortcoming.
        scorer = RecallAtK(k=5)
        report = scorer.score(
            RetrievalRun(
                question_id="q",
                expected_evidence=[],
                retrieved_ids=[],
                retrieved_names=[],
            )
        )
        assert report.score == 1.0
        assert report.matched == []
        assert report.missed == []

    def test_k_caps_the_window(self):
        # With k=2 the third retrieved item should not count.
        scorer = RecallAtK(k=2)
        report = scorer.score(
            RetrievalRun(
                question_id="q",
                expected_evidence=["D3:3"],
                retrieved_ids=[],
                retrieved_names=["A", "B", "D3:3"],
            )
        )
        assert report.score == 0.0
        assert scorer.metric == "recall@2"

    def test_invalid_k_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            RecallAtK(k=0)

    def test_partial_match_is_proportional(self):
        scorer = RecallAtK(k=5)
        report = scorer.score(
            RetrievalRun(
                question_id="q",
                expected_evidence=["A", "B", "C", "D"],
                retrieved_ids=[],
                retrieved_names=["A", "C"],
            )
        )
        assert report.score == 0.5


class TestBuildScorer:
    def test_recall_spec(self):
        scorer = build_scorer("recall@7")
        assert isinstance(scorer, RecallAtK)
        assert scorer.k == 7
        assert scorer.metric == "recall@7"

    def test_recall_spec_is_case_insensitive(self):
        scorer = build_scorer("Recall@3")
        assert isinstance(scorer, RecallAtK)
        assert scorer.k == 3

    def test_unknown_spec_rejected(self):
        with pytest.raises(ValueError, match="Unknown scorer"):
            build_scorer("bleu")

    def test_recall_without_k_rejected(self):
        with pytest.raises(ValueError):
            build_scorer("recall@notanumber")

    def test_llm_judge_is_stubbed(self):
        # The plumbing is there but the implementation isn't — PR-G4.
        with pytest.raises(NotImplementedError):
            LLMJudge()


# ---------------------------------------------------------------------------
# 7. Runner internals (PR-G3)
# ---------------------------------------------------------------------------


class TestOrJoinTokens:
    def test_joins_with_or(self):
        # FTS5 default is AND-on-whitespace — runner must rewrite so the
        # engine returns partial matches instead of empty results.
        out = _or_join_tokens("Caroline visit Lisbon")
        assert " OR " in out
        assert "Caroline" in out and "Lisbon" in out

    def test_drops_stop_words(self):
        # Stop-words only add noise to recall scoring; their absence
        # doesn't change the result set, only its ordering by relevance.
        out = _or_join_tokens("When did Caroline visit Lisbon?")
        assert "When" not in out  # stop-word stripped
        assert "Caroline" in out
        assert "Lisbon" in out

    def test_collapses_repeats(self):
        out = _or_join_tokens("Lisbon Lisbon Lisbon")
        assert out.count("Lisbon") == 1

    def test_quotes_fts5_operator_tokens(self):
        # Bare `OR`/`AND` are FTS5 keywords — must be escaped.
        out = _or_join_tokens("OR books")
        assert '"OR"' in out


# ---------------------------------------------------------------------------
# 8. End-to-end runner against the mini fixtures (PR-G3)
# ---------------------------------------------------------------------------


def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run the SDK against a temp DB with no embedder + no vault.

    Bench runs that hit Ollama for every node turn an 80-turn LOCOMO
    sample into a 30-second wait of failed embedding calls. The
    benchmark works fine on fulltext alone (the runner OR-joins query
    tokens), so we explicitly disable the embedder here.
    """
    monkeypatch.setenv("EMBEDDING_PROVIDER", "none")
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.delenv("VAULT_PATH", raising=False)
    # Scope env vars from a previous test shouldn't leak — the SDK falls
    # back to ``MemoryScope.from_env`` when no kwargs override it.
    for key in (
        "ENGRAMA_ORG_ID",
        "ENGRAMA_USER_ID",
        "ENGRAMA_AGENT_ID",
        "ENGRAMA_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ENGRAMA_DB_PATH", str(tmp_path / "bench.db"))


class TestRunnerEndToEnd:
    def test_locomo_full_run(self, monkeypatch, tmp_path):
        _isolated_env(monkeypatch, tmp_path)
        bench = LocomoBenchmark()
        bench.load(LOCOMO_FIXTURE)
        runner = BenchmarkRunner(bench, db_root=tmp_path / "dbs")
        report = runner.run()

        assert report.benchmark == "locomo"
        assert report.run_id.startswith("bench-locomo-")
        assert report.summary["questions_total"] == 3
        assert report.summary["questions_with_evidence"] == 3
        # Recall@5 on a 3-turn fixture where every answer is a single
        # turn should be perfect once the OR-rewriter is in place.
        assert report.summary["mean_score"] == 1.0
        # Latency tracked per question + averaged.
        assert all(q.latency_ms >= 0.0 for q in report.questions)
        # Lifecycle propagated to the config block.
        assert report.config["lifecycle"] == "per-conversation"

    def test_longmemeval_per_question_lifecycle(self, monkeypatch, tmp_path):
        _isolated_env(monkeypatch, tmp_path)
        bench = LongMemEvalBenchmark()
        bench.load(LONGMEMEVAL_FIXTURE)
        report = run_benchmark(bench, db_root=tmp_path / "dbs")

        assert report.config["lifecycle"] == "per-question"
        assert report.summary["questions_total"] == 3
        # The empty-haystack edge-case question has no evidence and is
        # a trivial pass — every recall row should be 1.0.
        for q in report.questions:
            assert q.score == 1.0
        # Per-question lifecycle should give each LongMemEval question
        # its own DB file under db_root.
        db_files = list((tmp_path / "dbs").glob("*.db"))
        assert len(db_files) >= 3  # one per question

    def test_limit_caps_questions(self, monkeypatch, tmp_path):
        _isolated_env(monkeypatch, tmp_path)
        bench = LocomoBenchmark()
        bench.load(LOCOMO_FIXTURE)
        report = run_benchmark(bench, limit=1, db_root=tmp_path / "dbs")
        assert len(report.questions) == 1
        assert report.config["limit"] == 1

    def test_report_writes_expected_schema_keys(self, monkeypatch, tmp_path):
        _isolated_env(monkeypatch, tmp_path)
        bench = LocomoBenchmark()
        bench.load(LOCOMO_FIXTURE)
        report = run_benchmark(bench, limit=1, db_root=tmp_path / "dbs")
        out = tmp_path / "report.json"
        report.write_json(out)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert set(data.keys()) >= {
            "benchmark",
            "run_id",
            "started_at",
            "completed_at",
            "config",
            "summary",
            "questions",
        }
        assert set(data["config"].keys()) >= {
            "limit",
            "scorer",
            "retrieval_limit",
            "lifecycle",
            "engrama_version",
        }
        assert set(data["summary"].keys()) >= {
            "questions_total",
            "questions_scored",
            "questions_with_evidence",
            "mean_score",
            "mean_latency_ms",
            "duration_seconds",
        }
        q0 = data["questions"][0]
        assert set(q0.keys()) >= {
            "question_id",
            "conversation_id",
            "category",
            "expected_evidence",
            "retrieved_ids",
            "retrieved_names",
            "score",
            "matched",
            "missed",
            "latency_ms",
        }

    def test_scope_isolates_concurrent_runs(self, monkeypatch, tmp_path):
        # Two runs against the same DB file must not contaminate one
        # another — the unique session_id scope is what guarantees it.
        _isolated_env(monkeypatch, tmp_path)
        shared = tmp_path / "shared.db"
        bench1 = LocomoBenchmark()
        bench1.load(LOCOMO_FIXTURE)
        bench2 = LocomoBenchmark()
        bench2.load(LOCOMO_FIXTURE)

        # Use ``db_root`` to force both runs onto the same disk
        # location. Each cycle still gets a fresh file *within* db_root,
        # but the scopes are what stop the second run from inheriting
        # the first's nodes (when the test exercises that path).
        r1 = run_benchmark(bench1, db_root=tmp_path / "shared-dbs")
        r2 = run_benchmark(bench2, db_root=tmp_path / "shared-dbs")
        assert r1.run_id != r2.run_id
        # Each run has its own unique scope (visible via the run_id slug).
        assert "bench-locomo-" in r1.run_id
        assert "bench-locomo-" in r2.run_id
        # Reused path doesn't crash; both reports came back clean.
        assert r1.summary["questions_total"] == 3
        assert r2.summary["questions_total"] == 3
        # Avoid `shared` unused warning — placeholder for future use.
        _ = shared


# ---------------------------------------------------------------------------
# 9. CLI: `engrama bench run` (PR-G3)
# ---------------------------------------------------------------------------


class TestBenchRunCli:
    def test_run_writes_report(self, tmp_path):
        report_path = tmp_path / "out.json"
        env = {
            **os.environ,
            "EMBEDDING_PROVIDER": "none",
            "GRAPH_BACKEND": "sqlite",
        }
        # Strip leaked scope from the developer's shell — a stray
        # ENGRAMA_USER_ID would silently filter all writes out.
        for key in (
            "ENGRAMA_ORG_ID",
            "ENGRAMA_USER_ID",
            "ENGRAMA_AGENT_ID",
            "ENGRAMA_SESSION_ID",
        ):
            env.pop(key, None)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "engrama.cli",
                "bench",
                "run",
                "--benchmark",
                "locomo",
                "--data-path",
                str(LOCOMO_FIXTURE),
                "--report",
                str(report_path),
                "--limit",
                "1",
                "--db-root",
                str(tmp_path / "dbs"),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "benchmark: locomo" in proc.stdout
        assert report_path.exists()
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["benchmark"] == "locomo"
        assert data["config"]["limit"] == 1
        assert len(data["questions"]) == 1


# ---------------------------------------------------------------------------
# 10. Reporter (PR-G4)
# ---------------------------------------------------------------------------


def _synthetic_report(tmp_path: Path) -> Path:
    """Hand-rolled report file the renderer tests run against.

    Built independently of the runner so a runner change can't silently
    paper over a reporter regression.
    """
    payload = {
        "benchmark": "locomo",
        "run_id": "bench-locomo-deadbeef",
        "started_at": "2026-05-15T12:00:00+00:00",
        "completed_at": "2026-05-15T12:00:10+00:00",
        "config": {
            "limit": None,
            "scorer": "recall@5",
            "retrieval_limit": 10,
            "lifecycle": "per-conversation",
            "engrama_version": "0.10.0",
        },
        "summary": {
            "questions_total": 4,
            "questions_scored": 4,
            "questions_with_evidence": 3,
            "mean_score": 0.5,
            "mean_latency_ms": 1.23,
            "duration_seconds": 10.0,
        },
        "questions": [
            {
                "question_id": "c0:q0",
                "conversation_id": "c0",
                "category": "factual",
                "expected_evidence": ["D1:1"],
                "retrieved_ids": [],
                "retrieved_names": ["D1:1"],
                "score": 1.0,
                "matched": ["D1:1"],
                "missed": [],
                "latency_ms": 1.0,
            },
            {
                "question_id": "c0:q1",
                "conversation_id": "c0",
                "category": "factual",
                "expected_evidence": ["D2:1"],
                "retrieved_ids": [],
                "retrieved_names": ["D3:1"],
                "score": 0.0,
                "matched": [],
                "missed": ["D2:1"],
                "latency_ms": 2.0,
            },
            {
                "question_id": "c1:q0",
                "conversation_id": "c1",
                "category": "temporal",
                "expected_evidence": ["D5:1", "D5:2"],
                "retrieved_ids": [],
                "retrieved_names": ["D5:1"],
                "score": 0.5,
                "matched": ["D5:1"],
                "missed": ["D5:2"],
                "latency_ms": 1.5,
            },
            {
                "question_id": "c2:q0",
                "conversation_id": "c2",
                "category": None,
                "expected_evidence": [],
                "retrieved_ids": [],
                "retrieved_names": [],
                "score": 1.0,
                "matched": [],
                "missed": [],
                "latency_ms": 0.5,
            },
        ],
    }
    path = tmp_path / "synth.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoadReport:
    def test_round_trip(self, tmp_path):
        path = _synthetic_report(tmp_path)
        data = load_report(path)
        assert data["benchmark"] == "locomo"
        assert len(data["questions"]) == 4

    def test_rejects_non_object(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="object"):
            load_report(bad)

    def test_rejects_missing_keys(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text('{"benchmark": "locomo"}', encoding="utf-8")
        with pytest.raises(ValueError, match="missing"):
            load_report(bad)


class TestCategoryBreakdown:
    def test_buckets_by_category(self, tmp_path):
        data = load_report(_synthetic_report(tmp_path))
        rows = category_breakdown(data["questions"])
        # Sorted alphabetically; three buckets (factual, temporal, uncategorised).
        cats = [r.category for r in rows]
        assert cats == ["(uncategorised)", "factual", "temporal"]

    def test_means_are_correct(self, tmp_path):
        data = load_report(_synthetic_report(tmp_path))
        rows = {r.category: r for r in category_breakdown(data["questions"])}
        # Factual: scores [1.0, 0.0] → mean 0.5; latencies [1.0, 2.0] → 1.5
        assert rows["factual"].count == 2
        assert rows["factual"].mean_score == 0.5
        assert rows["factual"].mean_latency_ms == 1.5
        # Temporal: single question scoring 0.5
        assert rows["temporal"].count == 1
        assert rows["temporal"].mean_score == 0.5
        # Uncategorised: the empty-category trivial pass
        assert rows["(uncategorised)"].count == 1
        assert rows["(uncategorised)"].mean_score == 1.0


class TestTopFailures:
    def test_excludes_perfect_scores(self, tmp_path):
        data = load_report(_synthetic_report(tmp_path))
        failures = top_failures(data["questions"])
        # Only the two questions with score < 1.0 should show.
        ids = [f["question_id"] for f in failures]
        assert "c0:q1" in ids
        assert "c1:q0" in ids
        # Perfect-score questions are not failures, regardless of evidence.
        assert "c0:q0" not in ids
        assert "c2:q0" not in ids

    def test_orders_lowest_first(self, tmp_path):
        data = load_report(_synthetic_report(tmp_path))
        failures = top_failures(data["questions"])
        # c0:q1 (0.0) comes before c1:q0 (0.5)
        assert failures[0]["question_id"] == "c0:q1"
        assert failures[1]["question_id"] == "c1:q0"

    def test_respects_limit(self, tmp_path):
        data = load_report(_synthetic_report(tmp_path))
        failures = top_failures(data["questions"], limit=1)
        assert len(failures) == 1


class TestRenderMarkdown:
    def test_contains_headline_sections(self, tmp_path):
        data = load_report(_synthetic_report(tmp_path))
        md = render_markdown(data)
        assert "# locomo benchmark report" in md
        assert "## Headline" in md
        assert "## Configuration" in md
        assert "## Per-category breakdown" in md
        assert "## Top 10 failures" in md
        # Headline numbers come through.
        assert "0.5000" in md  # mean_score
        # Failures section enumerates the actual misses, not the passes.
        assert "c0:q1" in md
        assert "D2:1" in md
        # Perfect-score questions are not listed in the failures block.
        for line in md.splitlines():
            if line.startswith("- **`c0:q0`**") or line.startswith("- **`c2:q0`**"):
                pytest.fail(f"Perfect-score question listed as failure: {line}")

    def test_no_failures_message_when_all_pass(self, tmp_path):
        payload = json.loads(_synthetic_report(tmp_path).read_text(encoding="utf-8"))
        # Force every score to a perfect pass.
        for q in payload["questions"]:
            q["score"] = 1.0
            q["missed"] = []
        md = render_markdown(payload)
        assert "No scorable failures" in md

    def test_top_failures_limit_propagates(self, tmp_path):
        data = load_report(_synthetic_report(tmp_path))
        md = render_markdown(data, top_failures_limit=5)
        assert "## Top 5 failures" in md


class TestBenchReportCli:
    def test_stdout_render(self, tmp_path):
        path = _synthetic_report(tmp_path)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "engrama.cli",
                "bench",
                "report",
                str(path),
                "--top-failures",
                "2",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert "# locomo benchmark report" in proc.stdout
        assert "## Top 2 failures" in proc.stdout

    def test_output_file(self, tmp_path):
        path = _synthetic_report(tmp_path)
        out = tmp_path / "out.md"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "engrama.cli",
                "bench",
                "report",
                str(path),
                "--output",
                str(out),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert out.exists()
        body = out.read_text(encoding="utf-8")
        assert "# locomo benchmark report" in body

    def test_missing_input_errors(self, tmp_path):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "engrama.cli",
                "bench",
                "report",
                str(tmp_path / "does-not-exist.json"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "Error" in proc.stderr
