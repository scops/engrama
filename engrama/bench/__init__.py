"""Benchmark harness for engrama (Roadmap P15 / DDR-003 Part 7).

Datasets covered by this package — one loader module each:

* :mod:`engrama.bench.locomo` — LOCOMO long-conversation memory (1,986 Q,
  10 conversations from snap-stanford/locomo).
* :mod:`engrama.bench.longmemeval` — LongMemEval (500 Q) is the next
  loader on the roadmap (PR-G2).

PR-G1 ships the loaders and the ``engrama bench list`` CLI subcommand so
the dataset shape can be inspected without engrama touching the data.
The runner (replay conversation → query → score) and the LLM-as-judge
scoring path land in PR-G3.
"""

from __future__ import annotations

from engrama.bench.core import Benchmark, BenchmarkConversation, BenchmarkQuestion
from engrama.bench.locomo import LocomoBenchmark

__all__ = [
    "Benchmark",
    "BenchmarkConversation",
    "BenchmarkQuestion",
    "LocomoBenchmark",
]
