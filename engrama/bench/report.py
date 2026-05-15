"""Benchmark report renderer (Roadmap P15 / DDR-003 Part 7).

Reads the JSON output of :class:`engrama.bench.runner.BenchmarkRunner`
and renders a human-friendly markdown summary: headline score, per-
category breakdown, latency, and a configurable top-N of failed
questions for debugging.

The schema this consumes is the frozen contract produced by PR-G3 —
see :meth:`BenchmarkReport.to_dict` for the field layout.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CategoryStat:
    """Per-category aggregate row."""

    category: str
    count: int
    mean_score: float
    mean_latency_ms: float


def load_report(path: str | Path) -> dict[str, Any]:
    """Read a report JSON written by ``engrama bench run``.

    Validates the top-level shape so a stale or hand-edited file
    surfaces a clear error instead of crashing deep inside the
    renderer.
    """
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Report must be a JSON object; got {type(data).__name__}")
    missing = [k for k in ("benchmark", "summary", "questions") if k not in data]
    if missing:
        raise ValueError(f"Report at {path!s} is missing required keys: {missing}")
    return data


def category_breakdown(questions: Iterable[dict[str, Any]]) -> list[CategoryStat]:
    """Aggregate per-category mean score + latency.

    Questions whose ``category`` is ``None`` or missing are grouped
    under ``"(uncategorised)"`` so the breakdown always tallies to the
    full question count.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for q in questions:
        cat = q.get("category") or "(uncategorised)"
        buckets.setdefault(cat, []).append(q)

    rows: list[CategoryStat] = []
    for category, items in buckets.items():
        n = len(items)
        mean_score = sum(float(q.get("score", 0.0)) for q in items) / n
        mean_latency = sum(float(q.get("latency_ms", 0.0)) for q in items) / n
        rows.append(
            CategoryStat(
                category=category,
                count=n,
                mean_score=mean_score,
                mean_latency_ms=mean_latency,
            )
        )
    rows.sort(key=lambda r: r.category)
    return rows


def top_failures(
    questions: Iterable[dict[str, Any]],
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Pick the lowest-scoring questions, breaking ties by ``question_id``.

    Anything that scored 1.0 is excluded — perfect recall isn't a
    failure even when there was evidence to find, and listing it
    drowns the actual misses in noise.
    """
    candidates = [q for q in questions if float(q.get("score", 0.0)) < 1.0]
    candidates.sort(key=lambda q: (float(q.get("score", 0.0)), str(q.get("question_id", ""))))
    return candidates[:limit]


def render_markdown(
    report: dict[str, Any],
    *,
    top_failures_limit: int = 10,
) -> str:
    """Render ``report`` (already-parsed JSON) to a markdown string.

    Top-level sections:

    1. Headline — benchmark, run id, mean score, latency, duration.
    2. Configuration — the run's CLI args (so a reader can reproduce).
    3. Per-category breakdown — table.
    4. Top failures — bulleted list with question text + missed evidence.
    """
    summary = report.get("summary", {})
    config = report.get("config", {})
    questions = report.get("questions", [])

    lines: list[str] = []
    lines.append(f"# {report.get('benchmark', '?')} benchmark report")
    lines.append("")
    lines.append(f"- Run id: `{report.get('run_id', '?')}`")
    lines.append(f"- Started: `{report.get('started_at', '?')}`")
    lines.append(f"- Completed: `{report.get('completed_at', '?')}`")
    lines.append(f"- Scorer: `{config.get('scorer', '?')}`")
    lines.append("")

    lines.append("## Headline")
    lines.append("")
    lines.append(f"- **Mean score:** {summary.get('mean_score', 0):.4f}")
    lines.append(
        f"- Questions scored: {summary.get('questions_scored', 0)} "
        f"(with evidence: {summary.get('questions_with_evidence', 0)})"
    )
    lines.append(f"- Mean latency: {summary.get('mean_latency_ms', 0):.2f} ms")
    lines.append(f"- Duration: {summary.get('duration_seconds', 0):.2f} s")
    lines.append("")

    lines.append("## Configuration")
    lines.append("")
    lines.append("| Key | Value |")
    lines.append("|-----|-------|")
    for key in sorted(config.keys()):
        lines.append(f"| `{key}` | `{config[key]}` |")
    lines.append("")

    lines.append("## Per-category breakdown")
    lines.append("")
    rows = category_breakdown(questions)
    if not rows:
        lines.append("_No questions._")
    else:
        lines.append("| Category | Count | Mean score | Mean latency (ms) |")
        lines.append("|----------|------:|-----------:|------------------:|")
        for row in rows:
            lines.append(
                f"| {row.category} | {row.count} | "
                f"{row.mean_score:.4f} | {row.mean_latency_ms:.2f} |"
            )
    lines.append("")

    lines.append(f"## Top {top_failures_limit} failures")
    lines.append("")
    failures = top_failures(questions, limit=top_failures_limit)
    if not failures:
        lines.append("_No scorable failures — every question with evidence scored 1.0._")
    else:
        for q in failures:
            qid = q.get("question_id", "?")
            score = float(q.get("score", 0.0))
            missed = q.get("missed") or []
            missed_str = ", ".join(missed) if missed else "—"
            lines.append(f"- **`{qid}`** — score {score:.2f}, missed: {missed_str}")
    lines.append("")

    return "\n".join(lines)


__all__ = [
    "CategoryStat",
    "category_breakdown",
    "load_report",
    "render_markdown",
    "top_failures",
]
