#!/usr/bin/env python3
"""Spec 001 T001 / SC-5 — micro-benchmark for search + context latency.

The LOCOMO/LongMemEval mini fixtures shipped in the repo only have 3
questions each, so the p95 they produce is the same number as their
p50 — not useful as a regression anchor. This script seeds a tmp
SQLite DB with a realistic N=500 nodes plus a small set of relations,
runs N_QUERIES search calls and N_QUERIES context calls, and prints
p50/p95/p99 latencies for each.

Run before any change that touches the read path and save the output
to ``specs/001-tenant-scoped-memory/baseline.md``. Re-run after the
change and compare; SC-5 says fail if p95 regresses by more than 10%.

Defaults are tuned for a ~10-second total runtime; raise ``--queries``
on a beefier dev box to tighten the percentile estimates.
"""

from __future__ import annotations

import argparse
import json
import random
import string
import sys
import tempfile
import time
from pathlib import Path

from engrama import Engrama


def _rand_word(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


def _seed(eng: Engrama, n_nodes: int, seed: int) -> list[tuple[str, str]]:
    """Seed n_nodes Project/Technology/Concept rows + a handful of relations
    and return ``[(label, name), ...]`` so the bench loop can pick query
    targets that actually exist (otherwise we measure miss latency).
    """
    rng = random.Random(seed)
    labels = ["Project", "Technology", "Concept", "Person"]
    names: list[tuple[str, str]] = []
    for _ in range(n_nodes):
        label = rng.choice(labels)
        name = f"{_rand_word(6)}-{rng.randint(1000, 9999)}"
        eng.remember(
            label,
            name,
            observation=f"micro-bench {label} {name}",
            tags=[_rand_word(4), _rand_word(4)],
        )
        names.append((label, name))

    # A small splat of relations so context() has neighbours to walk.
    valid_relations = [
        ("Project", "USES", "Technology"),
        ("Project", "APPLIES", "Concept"),
        ("Technology", "APPLIES", "Concept"),
        ("Person", "PERFORMS", "Project"),
    ]
    by_label: dict[str, list[str]] = {}
    for label, name in names:
        by_label.setdefault(label, []).append(name)
    for from_lbl, rel, to_lbl in valid_relations:
        fs = by_label.get(from_lbl, [])
        ts = by_label.get(to_lbl, [])
        if not fs or not ts:
            continue
        for _ in range(min(50, len(fs))):
            fn = rng.choice(fs)
            tn = rng.choice(ts)
            try:
                eng.associate(fn, from_lbl, rel, tn, to_lbl)
            except Exception:
                pass
    return names


def _percentile(sorted_values: list[float], p: float) -> float:
    n = len(sorted_values)
    if n == 0:
        return 0.0
    idx = max(0, min(n - 1, int(round(p * n)) - 1))
    return sorted_values[idx]


def _measure(label: str, fn, count: int) -> dict[str, float]:
    """Call ``fn()`` ``count`` times, return p50/p95/p99 in ms."""
    samples: list[float] = []
    for _ in range(count):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return {
        "op": label,
        "samples": count,
        "mean_ms": round(sum(samples) / count, 3),
        "p50_ms": round(_percentile(samples, 0.50), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "p99_ms": round(_percentile(samples, 0.99), 3),
    }


def _bench_search(db_path: Path, queries: list[str], *, graph_rerank: bool) -> dict[str, float]:
    """Open a fresh engine over ``db_path`` and measure ``search`` latency.

    ``graph_rerank`` toggles the spec-002 node-distance stage via
    ``ENGRAMA_GRAPH_RERANK`` (read at ``HybridConfig`` construction), so the
    two passes differ only by that stage — their delta isolates its cost.
    """
    import os

    os.environ["ENGRAMA_GRAPH_RERANK"] = "1" if graph_rerank else "0"
    with Engrama(backend="sqlite", db_path=db_path, org_id="bench", user_id="bench") as eng:
        for _ in range(20):  # warm-up: connection + first FTS5 query
            eng.search("warmup", limit=5)
        it = iter(queries)
        stats = _measure("search", lambda: eng.search(next(it), limit=10), len(queries))
    stats["graph_rerank"] = graph_rerank
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--nodes", type=int, default=500)
    p.add_argument("--queries", type=int, default=200)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--db-path",
        default=None,
        help="Persistent path for the seeded SQLite DB; default = tmp",
    )
    p.add_argument(
        "--report",
        default=None,
        help="Where to write the JSON report (default: stdout only)",
    )
    args = p.parse_args()

    rng = random.Random(args.seed)

    # Hermetic env so the bench doesn't pick up the user's real ~/.engrama.
    import os

    os.environ["EMBEDDING_PROVIDER"] = "null"

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(args.db_path) if args.db_path else Path(td) / "bench.db"
        with Engrama(
            backend="sqlite",
            db_path=db_path,
            org_id="bench",
            user_id="bench",
        ) as eng:
            print(f"Seeding {args.nodes} nodes …", file=sys.stderr)
            seeded = _seed(eng, args.nodes, args.seed)
            print(
                f"Seeded {len(seeded)} nodes; running {args.queries} queries each",
                file=sys.stderr,
            )

            # Pick a stable query corpus from the seeded names so search /
            # context measure hit-path latency, not miss latency.
            search_queries = [rng.choice(seeded)[1] for _ in range(args.queries)]
            context_targets = [rng.choice(seeded) for _ in range(args.queries)]

            # Warm-up so connection-init + first-FTS5-query don't pollute p99.
            for _ in range(20):
                eng.search("warmup", limit=5)

            it_search = iter(search_queries)
            it_ctx = iter(context_targets)

            search_stats = _measure(
                "search",
                lambda: eng.search(next(it_search), limit=10),
                args.queries,
            )
            context_stats = _measure(
                "context",
                lambda: eng._engine.get_context(next(it_ctx)[1], next(it_ctx)[0], hops=1),
                args.queries // 2,  # context is heavier; half the load
            )

        # --- Spec 002 SC-003: cost of the graph-rerank stage ---
        # Re-run search with the stage on vs off over the same seeded DB and
        # report the added latency. Budget: added median < 50ms, p95 < 150ms
        # (< 300ms pathological).
        rerank_queries = [rng.choice(seeded)[1] for _ in range(args.queries)]
        on = _bench_search(db_path, rerank_queries, graph_rerank=True)
        off = _bench_search(db_path, rerank_queries, graph_rerank=False)
        added_p50 = round(on["p50_ms"] - off["p50_ms"], 3)
        added_p95 = round(on["p95_ms"] - off["p95_ms"], 3)
        graph_rerank_stats = {
            "search_rerank_on": on,
            "search_rerank_off": off,
            "added_p50_ms": added_p50,
            "added_p95_ms": added_p95,
            "budget": {"median_ms": 50, "p95_ms": 150, "pathological_ms": 300},
            "within_budget": added_p50 < 50 and added_p95 < 150,
        }

    report = {
        "n_nodes": args.nodes,
        "n_queries": args.queries,
        "seed": args.seed,
        "search": search_stats,
        "context": context_stats,
        "graph_rerank": graph_rerank_stats,
    }
    out = json.dumps(report, indent=2)
    print(out)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(out, encoding="utf-8")
    verdict = "PASS" if graph_rerank_stats["within_budget"] else "OVER BUDGET"
    print(
        f"graph-rerank added latency: p50 {added_p50}ms / p95 {added_p95}ms — {verdict}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
