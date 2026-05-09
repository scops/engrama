"""
engrama/skills/recall.py

The recall skill combines fulltext search with graph traversal to produce
rich, contextual results.  It is the primary "what do I know about X?"
entry point for agents.

Pipeline:

1. **Fulltext search** — runs against the ``memory_search`` index to find
   seed nodes matching the query string.
2. **Graph expansion** — for each seed node, traverses up to *hops*
   relationships outward, collecting neighbours with their relationship
   types and directions.
3. **Deduplication** — neighbours that appear in multiple expansions are
   merged, keeping the shortest path.

The result is a list of :class:`RecallResult` dicts, each containing the
seed node plus its neighbourhood.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from engrama.core.schema import TITLE_KEYED_LABELS

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine


@dataclass
class RecallResult:
    """One seed node and its neighbourhood from a recall query."""

    label: str
    name: str
    score: float
    properties: dict[str, Any] = field(default_factory=dict)
    neighbours: list[dict[str, Any]] = field(default_factory=list)


class RecallSkill:
    """Fulltext search + graph traversal recall.

    Combines :meth:`EngramaEngine.search` with
    :meth:`EngramaEngine.get_context` to return seed nodes together with
    their local neighbourhood.
    """

    def run(
        self,
        engine: "EngramaEngine",
        *,
        query: str,
        limit: int = 5,
        hops: int = 2,
    ) -> list[RecallResult]:
        """Search the graph and expand each hit with its neighbourhood.

        Args:
            engine: An initialised :class:`EngramaEngine`.
            query: Lucene-syntax search string.
            limit: Max seed nodes to return from fulltext search.
            hops: Neighbourhood depth for graph expansion.

        Returns:
            A list of :class:`RecallResult` ordered by fulltext score.
        """
        # Step 1 — fulltext search for seed nodes
        search_records = engine.search(query, limit=limit)

        results: list[RecallResult] = []
        seen_names: set[str] = set()

        for record in search_records:
            label = record["type"]
            name = record["name"]
            score = record["score"]

            # Deduplicate seeds (same node can appear if name matches
            # multiple indexed properties).
            if name in seen_names:
                continue
            seen_names.add(name)

            # Step 2 — retrieve full node properties
            merge_key = "title" if label in TITLE_KEYED_LABELS else "name"
            properties = engine._store.get_node(label, merge_key, name) or {}

            # Step 3 — expand neighbourhood
            neighbours: list[dict[str, Any]] = []
            try:
                ctx_records = engine.get_context(name, label, hops=hops)
                neighbour_ids: set[str] = set()
                for ctx in ctx_records:
                    neighbour_node = ctx["neighbour"]
                    if not neighbour_node:
                        continue
                    nid = neighbour_node["_id"]
                    if nid in neighbour_ids:
                        continue
                    neighbour_ids.add(nid)

                    n_labels = neighbour_node.get("_labels", [])
                    n_label = n_labels[0] if n_labels else "Unknown"
                    n_key = "title" if n_label in TITLE_KEYED_LABELS else "name"
                    n_name = neighbour_node.get(n_key, "?")

                    rels = ctx["rel"]
                    if isinstance(rels, list):
                        rel_types = [r["_type"] for r in rels]
                    else:
                        rel_types = [rels["_type"]]

                    neighbours.append({
                        "label": n_label,
                        "name": n_name,
                        "rel_chain": rel_types,
                        "properties": {
                            k: v for k, v in neighbour_node.items()
                            if not k.startswith("_")
                        },
                    })
            except Exception:
                # get_context may fail if node was deleted between search
                # and expansion — skip gracefully.
                pass

            results.append(RecallResult(
                label=label,
                name=name,
                score=score,
                properties=properties,
                neighbours=neighbours,
            ))

        return results
