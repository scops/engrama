"""Graph health metrics (read-only).

Measures how well the memory graph holds together: orphans, stubs acting as
hubs, fragmentation into components, duplicate candidates, tags that name an
anchor node without an edge to it, archived nodes that still bridge live ones,
and schema completeness.

The computation is pure — it takes a scoped snapshot (``nodes`` + ``edges``,
see ``health_snapshot`` on the stores) and returns a JSON-serialisable report
— so it is backend-agnostic and testable without a database. Every metric is
computed inside the caller's scope: the snapshot is already scope-filtered.

Vocabulary used throughout:

* **system Insight** — an ``Insight`` produced by reflect (it carries
  ``source_query``). A hand-written ``Insight`` is an ordinary domain node.
* **live** — not archived and not a system Insight.
* **anchor** — a node other nodes are expected to link to by name
  (``Project``, ``Client``, ``Course``, ``Domain``).
"""

from __future__ import annotations

import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

ANCHOR_LABELS: frozenset[str] = frozenset({"Project", "Client", "Course", "Domain"})
HUB_STUB_MIN_DEGREE = 3
_TOP = 10


def normalise_key(value: str) -> str:
    """Casefold, strip accents, and unify ``-``/``_``/whitespace runs."""
    decomposed = unicodedata.normalize("NFKD", value)
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    spaced = no_accents.casefold().replace("-", " ").replace("_", " ")
    return " ".join(spaced.split())


def is_system_insight(node: dict[str, Any]) -> bool:
    return node.get("label") == "Insight" and bool(node.get("source_query"))


def _components(ids: set[Any], adjacency: dict[Any, set[Any]]) -> list[int]:
    seen: set[Any] = set()
    sizes: list[int] = []
    for start in ids:
        if start in seen:
            continue
        seen.add(start)
        stack, size = [start], 0
        while stack:
            node = stack.pop()
            size += 1
            for nxt in adjacency[node]:
                if nxt in ids and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        sizes.append(size)
    return sorted(sizes, reverse=True)


def _component_summary(ids: set[Any], adjacency: dict[Any, set[Any]]) -> dict[str, Any]:
    sizes = _components(ids, adjacency)
    largest = sizes[0] if sizes else 0
    return {
        "nodes": len(ids),
        "components": len(sizes),
        "largest": largest,
        "largest_pct": round(100 * largest / len(ids), 1) if ids else 0.0,
        "singletons": sum(1 for s in sizes if s == 1),
    }


def _tags(node: dict[str, Any]) -> list[str]:
    raw = node.get("tags")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def compute_health(
    nodes: Iterable[dict[str, Any]], edges: Iterable[tuple[Any, Any]]
) -> dict[str, Any]:
    """Compute the health report for one scoped snapshot.

    Each node dict carries ``id``, ``label``, ``key`` and, when present,
    ``status``, ``tags``, ``confidence``, ``source_query`` plus the booleans
    ``has_summary``, ``has_engrama_id``, ``has_source`` and ``has_trust``.
    ``edges`` are ``(id_a, id_b)`` pairs between nodes of the snapshot;
    direction and relation type don't matter here.
    """
    by_id = {n["id"]: n for n in nodes}
    system = {i for i, n in by_id.items() if is_system_insight(n)}
    # Structure is measured between domain nodes only: a system Insight's
    # ABOUT edges annotate the graph, they don't connect it.
    adjacency: dict[Any, set[Any]] = defaultdict(set)
    insight_links: Counter[Any] = Counter()
    for a, b in edges:
        if a not in by_id or b not in by_id or a == b:
            continue
        if a in system or b in system:
            insight_links[a] += 1
            insight_links[b] += 1
            continue
        adjacency[a].add(b)
        adjacency[b].add(a)

    def degree(nid: Any) -> int:
        return len(adjacency[nid])

    archived = {i for i, n in by_id.items() if n.get("status") == "archived"} - system
    live = set(by_id) - system - archived
    stubs = {i for i in live if by_id[i].get("status") == "stub"}

    orphans = [i for i in live if degree(i) == 0]
    buckets = Counter(
        "0" if d == 0 else "1" if d == 1 else "2-3" if d <= 3 else "4-9" if d <= 9 else "10+"
        for d in (degree(i) for i in live)
    )

    hub_stubs = sorted(
        (i for i in stubs if degree(i) >= HUB_STUB_MIN_DEGREE), key=degree, reverse=True
    )

    core = {i for i in live if i not in stubs and by_id[i].get("label") != "Domain"}

    # Duplicate candidates: normalised key collisions among non-system nodes.
    groups: dict[str, list[Any]] = defaultdict(list)
    for i in live | archived:
        key = by_id[i].get("key")
        if key:
            groups[normalise_key(key)].append(i)
    duplicates = sorted((ids for ids in groups.values() if len(ids) > 1), key=len, reverse=True)

    # Tags naming an anchor with no edge to it.
    anchors: dict[str, list[Any]] = defaultdict(list)
    for i in live:
        n = by_id[i]
        if n.get("label") in ANCHOR_LABELS and n.get("key"):
            anchors[normalise_key(n["key"])].append(i)
    unlinked: Counter[Any] = Counter()
    for i in live:
        for tag in {normalise_key(t) for t in _tags(by_id[i])}:
            for anchor in anchors.get(tag, ()):
                if anchor != i and anchor not in adjacency[i]:
                    unlinked[anchor] += 1

    bridges = sorted(
        (i for i in archived if any(nb in live for nb in adjacency[i])),
        key=lambda i: sum(1 for nb in adjacency[i] if nb in live),
        reverse=True,
    )

    def ref(i: Any) -> str:
        return f"{by_id[i].get('label')}:{by_id[i].get('key')}"

    conf = [
        by_id[i]["confidence"] for i in live if isinstance(by_id[i].get("confidence"), int | float)
    ]
    domain_live = [i for i in live if by_id[i].get("label") != "Insight"]

    return {
        "totals": {
            "nodes": len(by_id),
            "live": len(live),
            "archived": len(archived),
            "system_insights": len(system),
            "stubs": len(stubs),
            "edges": sum(len(v) for v in adjacency.values()) // 2,
        },
        "orphans": {
            "live": len(orphans),
            "live_pct": round(100 * len(orphans) / len(live), 1) if live else 0.0,
            "by_label": dict(Counter(by_id[i]["label"] for i in orphans).most_common()),
            "system_insights": sum(1 for i in system if not insight_links[i]),
            "domain_pct": round(
                100 * sum(1 for i in domain_live if degree(i) == 0) / len(domain_live), 1
            )
            if domain_live
            else 0.0,
        },
        "degree": {b: buckets.get(b, 0) for b in ("0", "1", "2-3", "4-9", "10+")},
        "hub_stubs": {
            "count": len(hub_stubs),
            "top": [
                {"node": ref(i), "degree": degree(i), "has_summary": by_id[i].get("has_summary")}
                for i in hub_stubs[:_TOP]
            ],
        },
        "components": {
            "live": _component_summary(live, adjacency),
            "core": _component_summary(core, adjacency),
        },
        "duplicates": {
            "groups": len(duplicates),
            "top": [[ref(i) for i in ids] for ids in duplicates[:_TOP]],
        },
        "tags_without_edge": {
            "nodes": sum(unlinked.values()),
            "top": [{"anchor": ref(a), "unlinked": c} for a, c in unlinked.most_common(_TOP)],
        },
        "archived": {
            "count": len(archived),
            "hubs": sum(1 for i in archived if degree(i) >= HUB_STUB_MIN_DEGREE),
            "bridges": len(bridges),
            "top_bridges": [
                {"node": ref(i), "live_neighbours": sum(1 for nb in adjacency[i] if nb in live)}
                for i in bridges[:_TOP]
            ],
        },
        "schema": {
            "missing_engrama_id": sum(1 for n in by_id.values() if not n.get("has_engrama_id")),
            "missing_source": sum(1 for n in by_id.values() if not n.get("has_source")),
            "missing_trust_level": sum(1 for n in by_id.values() if not n.get("has_trust")),
            "status_values": dict(
                Counter(by_id[i].get("status") or "<none>" for i in live).most_common()
            ),
        },
        "confidence": {
            "live_with_value": len(conf),
            "below_0_3": sum(1 for c in conf if c < 0.3),
            "below_0_05": sum(1 for c in conf if c < 0.05),
        },
    }


def format_health(report: dict[str, Any]) -> str:
    """Render a report as a compact, human-readable text block."""
    t, o, c = report["totals"], report["orphans"], report["components"]
    lines = [
        f"Nodes: {t['nodes']} ({t['live']} live, {t['archived']} archived, "
        f"{t['system_insights']} system Insights, {t['stubs']} stubs), edges: {t['edges']}",
        f"Orphans (live, degree 0): {o['live']} ({o['live_pct']}%) — domain only: "
        f"{o['domain_pct']}%; system Insights linked to nothing: {o['system_insights']}",
        "Degree (live): " + ", ".join(f"{k}: {v}" for k, v in report["degree"].items()),
    ]
    for view in ("live", "core"):
        s = c[view]
        lines.append(
            f"Components ({view}): {s['components']}, largest {s['largest']} "
            f"({s['largest_pct']}%), singletons {s['singletons']}"
        )
    lines.append(f"Hub stubs (degree >= {HUB_STUB_MIN_DEGREE}): {report['hub_stubs']['count']}")
    lines += [f"  - {h['node']} (degree {h['degree']})" for h in report["hub_stubs"]["top"]]
    lines.append(f"Duplicate name groups: {report['duplicates']['groups']}")
    lines += [f"  - {' | '.join(g)}" for g in report["duplicates"]["top"]]
    tw = report["tags_without_edge"]
    lines.append(f"Tags naming an anchor without an edge: {tw['nodes']}")
    lines += [f"  - {x['anchor']}: {x['unlinked']}" for x in tw["top"]]
    a = report["archived"]
    lines.append(
        f"Archived: {a['count']} (hubs: {a['hubs']}, still bridging live nodes: {a['bridges']})"
    )
    lines += [f"  - {x['node']} ({x['live_neighbours']} live)" for x in a["top_bridges"]]
    s = report["schema"]
    lines.append(
        f"Schema gaps: engrama_id {s['missing_engrama_id']}, source {s['missing_source']}, "
        f"trust_level {s['missing_trust_level']}; status values: {len(s['status_values'])}"
    )
    cf = report["confidence"]
    lines.append(
        f"Confidence (live): {cf['below_0_3']} below 0.3, {cf['below_0_05']} below 0.05 "
        f"of {cf['live_with_value']}"
    )
    return "\n".join(lines)


__all__ = [
    "ANCHOR_LABELS",
    "compute_health",
    "format_health",
    "is_system_insight",
    "normalise_key",
]
