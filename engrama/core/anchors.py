"""Anchor nodes and tag anchoring (DDR-006 items 4 and 5).

An *anchor* is a node other nodes are expected to link to by name: a
``Project``, ``Client``, ``Course`` or ``Domain``. When a node carries a tag
that names an anchor it isn't linked to, the tag is standing in for an edge.
This module decides which edge that tag implies, and computes where the graph
has such tags without edges (for ``engrama health`` and reflect).

``ENGRAMA_TAG_LINKING`` controls what a write does with those tags:
``suggest`` (default) reports them as ``suggested_relations``, ``auto`` creates
the edges, ``off`` ignores them. ``ENGRAMA_REQUIRE_RELATIONS`` makes a write
that would leave a non-anchor node with no edge fail instead.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from engrama.core.names import normalise_key

logger = logging.getLogger("engrama.core.anchors")

# Anchor label → (relation type, direction). "out": node -[REL]-> anchor;
# "in": anchor -[REL]-> node.
ANCHOR_RELATIONS: dict[str, tuple[str, str]] = {
    "Project": ("BELONGS_TO", "out"),
    "Client": ("FOR", "out"),
    "Course": ("COVERS", "in"),
    "Domain": ("IN_DOMAIN", "out"),
}
ANCHOR_LABELS: frozenset[str] = frozenset(ANCHOR_RELATIONS)
# Labels that may legitimately exist without edges (the things others link to).
RELATION_EXEMPT_LABELS: frozenset[str] = ANCHOR_LABELS | {"Person"}

_TAG_MODES = ("off", "suggest", "auto")
_TRUTHY = {"1", "true", "yes", "on"}


def tag_linking_mode() -> str:
    """``ENGRAMA_TAG_LINKING``: ``off`` | ``suggest`` (default) | ``auto``."""
    raw = (os.environ.get("ENGRAMA_TAG_LINKING") or "suggest").strip().lower()
    if raw not in _TAG_MODES:
        logger.warning("Ignoring ENGRAMA_TAG_LINKING=%r; using 'suggest'", raw)
        return "suggest"
    return raw


def require_relations() -> bool:
    """``ENGRAMA_REQUIRE_RELATIONS``: reject writes that leave a node unlinked."""
    return (os.environ.get("ENGRAMA_REQUIRE_RELATIONS") or "").strip().lower() in _TRUTHY


def node_tags(props: Mapping[str, Any]) -> list[str]:
    raw = props.get("tags")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def anchor_relation(anchor_label: str) -> dict[str, str]:
    rel_type, direction = ANCHOR_RELATIONS[anchor_label]
    return {"rel_type": rel_type, "direction": direction}


def suggest_from_tags(
    label: str,
    name: str,
    tags: Iterable[str],
    anchors: Iterable[Mapping[str, str]],
    linked: set[tuple[str, str]],
) -> list[dict[str, str]]:
    """Relations implied by tags that name an anchor the node isn't linked to.

    ``anchors`` are ``{label, name}`` of the live anchor nodes in scope;
    ``linked`` holds the ``(label, name)`` of the node's current neighbours.
    """
    by_key: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for a in anchors:
        by_key[normalise_key(a["name"])].append(a)
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for tag in tags:
        for a in by_key.get(normalise_key(tag), ()):
            ref = (a["label"], a["name"])
            if ref == (label, name) or ref in linked or ref in seen:
                continue
            seen.add(ref)
            out.append(
                {"label": a["label"], "name": a["name"], "tag": tag, **anchor_relation(a["label"])}
            )
    return out


def unlinked_tag_anchors(
    nodes: Mapping[Any, Mapping[str, Any]], adjacency: Mapping[Any, set[Any]]
) -> list[dict[str, Any]]:
    """Anchors named by tags on nodes that have no edge to them.

    ``nodes`` maps id → node dict (``label``, ``key``, ``tags``); ``adjacency``
    maps id → neighbour ids. Returns ``[{label, name, nodes: [{label, name}]}]``
    for each such anchor, most unlinked first.
    """
    anchors: dict[str, list[Any]] = defaultdict(list)
    for i, n in nodes.items():
        if n.get("label") in ANCHOR_LABELS and n.get("key"):
            anchors[normalise_key(n["key"])].append(i)
    unlinked: dict[Any, list[Any]] = defaultdict(list)
    for i, n in nodes.items():
        for tag in {normalise_key(t) for t in node_tags(n)}:
            for a in anchors.get(tag, ()):
                if a != i and a not in adjacency.get(i, set()):
                    unlinked[a].append(i)
    rows = [
        {
            "label": nodes[a]["label"],
            "name": nodes[a]["key"],
            "nodes": sorted(
                ({"label": nodes[i]["label"], "name": nodes[i]["key"]} for i in ids),
                key=lambda x: (x["label"], x["name"]),
            ),
        }
        for a, ids in unlinked.items()
    ]
    return sorted(rows, key=lambda r: (-len(r["nodes"]), r["label"], r["name"]))


__all__ = [
    "ANCHOR_LABELS",
    "ANCHOR_RELATIONS",
    "RELATION_EXEMPT_LABELS",
    "anchor_relation",
    "node_tags",
    "require_relations",
    "suggest_from_tags",
    "tag_linking_mode",
    "unlinked_tag_anchors",
]
