"""Benchmark harness for engrama (Roadmap P15 / DDR-003 Part 7).

Datasets covered by this package — one loader module each:

* :mod:`engrama.bench.locomo` — LOCOMO long-conversation memory (1,986 Q,
  10 conversations from snap-stanford/locomo).
* :mod:`engrama.bench.longmemeval` — LongMemEval (500 Q from
  xiaowu0162/LongMemEval).

PR-G1 + PR-G2 shipped the loaders and the ``engrama bench list`` CLI
subcommand so the dataset shape can be inspected without engrama
touching the data. PR-G3 adds the runner (replay conversation → query
→ score) + recall-based scoring + the ``engrama bench run``
subcommand. PR-G4 will layer the LLM-as-judge path and a markdown
reporter on top.
"""

from __future__ import annotations

from engrama.bench.core import Benchmark, BenchmarkConversation, BenchmarkQuestion
from engrama.bench.locomo import LocomoBenchmark
from engrama.bench.longmemeval import LongMemEvalBenchmark
from engrama.bench.runner import (
    BenchmarkReport,
    BenchmarkRunner,
    QuestionResult,
    run_benchmark,
)
from engrama.bench.scoring import (
    RecallAtK,
    RetrievalRun,
    Scorer,
    ScoreReport,
    build_scorer,
)

__all__ = [
    "Benchmark",
    "BenchmarkConversation",
    "BenchmarkQuestion",
    "BenchmarkReport",
    "BenchmarkRunner",
    "LocomoBenchmark",
    "LongMemEvalBenchmark",
    "QuestionResult",
    "RecallAtK",
    "RetrievalRun",
    "ScoreReport",
    "Scorer",
    "build_scorer",
    "run_benchmark",
]
