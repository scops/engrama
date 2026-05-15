"""Benchmark harness for engrama (Roadmap P15 / DDR-003 Part 7).

Datasets covered by this package — one loader module each:

* :mod:`engrama.bench.locomo` — LOCOMO long-conversation memory (1,986 Q,
  10 conversations from snap-stanford/locomo).
* :mod:`engrama.bench.longmemeval` — LongMemEval (500 Q from
  xiaowu0162/LongMemEval).

PR-G1 + PR-G2 shipped the loaders and the ``engrama bench list`` CLI
subcommand. PR-G3 added the runner (replay → query → score) +
recall-based scoring + the ``engrama bench run`` subcommand. PR-G4
adds the markdown reporter (``engrama bench report``) on top of the
runner's JSON schema. LLM-as-judge is still stubbed — landing as a
follow-up when an operator has a provider configured.
"""

from __future__ import annotations

from engrama.bench.core import Benchmark, BenchmarkConversation, BenchmarkQuestion
from engrama.bench.locomo import LocomoBenchmark
from engrama.bench.longmemeval import LongMemEvalBenchmark
from engrama.bench.report import (
    CategoryStat,
    category_breakdown,
    load_report,
    render_markdown,
    top_failures,
)
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
    "CategoryStat",
    "LocomoBenchmark",
    "LongMemEvalBenchmark",
    "QuestionResult",
    "RecallAtK",
    "RetrievalRun",
    "ScoreReport",
    "Scorer",
    "build_scorer",
    "category_breakdown",
    "load_report",
    "render_markdown",
    "run_benchmark",
    "top_failures",
]
