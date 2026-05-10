"""
Engrama — Async wrapper around the SQLite graph + vector stores.

``SqliteAsyncStore`` mirrors :class:`Neo4jAsyncStore`'s public contract
so the MCP server can consume either backend interchangeably.

Why explicit methods (not ``__getattr__`` magic): the sync stores still
expose the legacy ``[{"n": <node-dict>}]`` records-style shape (kept for
backwards compatibility with the engine and the contract test suite),
while the async contract uses richer shapes (``{"node": ..., "created":
...}``, ``{label, name, via, properties}`` for neighbours, etc.).  This
class is the single place where that translation happens.

Each method delegates the actual work to the sync store via
:func:`asyncio.to_thread` (sqlite3 is CPU-bound and blocking; we run it
on the thread pool to avoid blocking the event loop).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from engrama.backends.sqlite.store import SqliteGraphStore
from engrama.backends.sqlite.vector import SqliteVecStore

logger = logging.getLogger("engrama.backends.sqlite.async_store")

T = TypeVar("T")


class SqliteAsyncStore:
    """Async ``GraphStore`` + ``VectorStore`` over SQLite.

    Parameters:
        path: Database path (or ``":memory:"``).
        vector_dimensions: Embedding dim. ``0`` disables vector ops.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        vector_dimensions: int = 0,
    ) -> None:
        self._sync = SqliteGraphStore(path)
        self._vector = SqliteVecStore(self._sync._conn, vector_dimensions)
        if vector_dimensions:
            self._vector.ensure_index()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def dimensions(self) -> int:
        """Embedding dimensionality (``VectorStore`` protocol)."""
        return self._vector.dimensions

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    async def _run(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(fn, *args, **kwargs)

    @staticmethod
    def _strip_internal(node: dict[str, Any]) -> dict[str, Any]:
        """Drop the legacy ``_id``/``_labels`` markers the sync store
        attaches; the async contract returns plain property dicts.
        """
        out = dict(node)
        out.pop("_id", None)
        out.pop("_labels", None)
        return out

    # ------------------------------------------------------------------
    # Node operations
    # ------------------------------------------------------------------

    async def merge_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        properties: dict[str, Any],
        embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Create or update a node. Always MERGE semantics.

        Returns ``{"node": <props>, "created": <bool>}`` (mirrors
        :meth:`Neo4jAsyncStore.merge_node`). ``created`` is inferred by
        comparing ``created_at`` and ``updated_at`` on the returned row.
        """
        rows = await self._run(
            self._sync.merge_node,
            label, key_field, key_value, properties,
        )
        if not rows:
            return {"node": {}, "created": False}
        n = self._strip_internal(rows[0]["n"])
        created = n.get("created_at") == n.get("updated_at")
        return {"node": n, "created": created}

    async def get_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._sync.get_node, label, key_field, key_value,
        )

    async def delete_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        soft: bool = True,
    ) -> bool:
        return await self._run(
            self._sync.delete_node, label, key_field, key_value, soft,
        )

    async def list_existing_nodes(self, limit: int = 200) -> list[dict[str, str]]:
        return await self._run(self._sync.list_existing_nodes, limit)

    # ------------------------------------------------------------------
    # Relationship operations
    # ------------------------------------------------------------------

    async def merge_relation(
        self,
        from_label: str,
        from_key: str,
        from_value: str,
        rel_type: str,
        to_label: str,
        to_key: str,
        to_value: str,
    ) -> dict[str, Any]:
        """Idempotent MERGE of a relationship.

        Returns ``{"rel_type", "from_name", "to_name",
        "from_obsidian_path"}`` on success, ``{}`` if either endpoint is
        missing — mirrors :meth:`Neo4jAsyncStore.merge_relation`.
        """
        rows = await self._run(
            self._sync.merge_relation,
            from_label, from_key, from_value,
            rel_type,
            to_label, to_key, to_value,
        )
        if not rows:
            return {}
        # The sync store doesn't echo the source obsidian_path, but the
        # MCP DDR-002 dual-write path needs it; cheap to fetch in-process.
        from_node = await self._run(
            self._sync.get_node, from_label, from_key, from_value,
        ) or {}
        return {
            "rel_type": rel_type,
            "from_name": from_value,
            "to_name": to_value,
            "from_obsidian_path": from_node.get("obsidian_path"),
        }

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    async def get_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Traverse N hops from a node. Returns the
        ``[{label, name, via, properties}]`` shape used by the MCP
        tools (mirrors :meth:`Neo4jAsyncStore.get_neighbours`).
        """
        rows = await self._run(
            self._sync.get_neighbours,
            label, key_field, key_value, hops, limit,
        )
        if not rows:
            return []

        # Identify the root so we don't include it as its own neighbour.
        start = rows[0]["start"]
        start_label = (start.get("_labels") or [label])[0]
        start_name = start.get("name") or start.get("title")
        root_key: tuple[str, str | None] = (start_label, start_name)

        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            n = row["neighbour"]
            n_label = (n.get("_labels") or ["Unknown"])[0]
            n_name = n.get("name") or n.get("title")
            if not n_name:
                continue
            key = (n_label, n_name)
            if key in seen or key == root_key:
                continue
            seen.add(key)
            via = list(dict.fromkeys(r["_type"] for r in row["rel"]))
            out.append({
                "label": n_label,
                "name": n_name,
                "via": via,
                "properties": {
                    k: v
                    for k, v in n.items()
                    if not k.startswith("_")
                    and k not in {"created_at", "updated_at", "details", "embedding"}
                },
            })
        return out

    async def get_node_with_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
    ) -> dict[str, Any] | None:
        """Convenience: ``get_node`` + ``get_neighbours`` in one call.

        The sync store already returns the ``{"node", "neighbours"}``
        shape that mirrors Neo4j async, so we just forward.
        """
        return await self._run(
            self._sync.get_node_with_neighbours,
            label, key_field, key_value, hops,
        )

    async def fulltext_search(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return await self._run(self._sync.fulltext_search, query, limit)

    async def count_labels(self) -> dict[str, int]:
        return await self._run(self._sync.count_labels)

    async def lookup_node_label(self, name: str) -> str | None:
        return await self._run(self._sync.lookup_node_label, name)

    async def run_pattern(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """SQLite has no Cypher engine; raises ``NotImplementedError``.

        The MCP reflect path uses the named ``detect_*`` methods rather
        than ``run_pattern``, so this is only here for protocol
        compliance with :meth:`Neo4jAsyncStore.run_pattern`.
        """
        return await self._run(self._sync.run_cypher, cypher, params)

    # ------------------------------------------------------------------
    # Insight operations
    # ------------------------------------------------------------------

    async def get_dismissed_titles(self) -> set[str]:
        # Sync method has a longer name (``get_dismissed_insight_titles``);
        # the async contract uses ``get_dismissed_titles`` to match Neo4j.
        return await self._run(self._sync.get_dismissed_insight_titles)

    async def get_approved_titles(self) -> set[str]:
        # Symmetric with get_dismissed_titles — sync has a longer name.
        return await self._run(self._sync.get_approved_insight_titles)

    async def get_pending_insights(self, limit: int = 10) -> list[dict[str, Any]]:
        return await self._run(self._sync.get_pending_insights, limit)

    async def get_insight_by_title(
        self, title: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._sync.get_insight_by_title, title)

    async def update_insight_status(
        self, title: str, new_status: str,
    ) -> bool:
        return await self._run(
            self._sync.update_insight_status, title, new_status,
        )

    async def mark_insight_synced(
        self, title: str, obsidian_path: str,
    ) -> bool:
        return await self._run(
            self._sync.mark_insight_synced, title, obsidian_path,
        )

    async def find_insight_by_source_query(
        self,
        source_query: str,
        statuses: list[str] | None = None,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._sync.find_insight_by_source_query, source_query, statuses,
        )

    # ------------------------------------------------------------------
    # Reflect — pattern detection
    # ------------------------------------------------------------------

    async def detect_cross_project_solutions(self) -> list[dict[str, Any]]:
        return await self._run(self._sync.detect_cross_project_solutions)

    async def detect_shared_technology(self) -> list[dict[str, Any]]:
        return await self._run(self._sync.detect_shared_technology)

    async def detect_training_opportunities(self) -> list[dict[str, Any]]:
        return await self._run(self._sync.detect_training_opportunities)

    async def detect_technique_transfer(self) -> list[dict[str, Any]]:
        return await self._run(self._sync.detect_technique_transfer)

    async def detect_concept_clusters(self) -> list[dict[str, Any]]:
        return await self._run(self._sync.detect_concept_clusters)

    async def detect_stale_knowledge(self) -> list[dict[str, Any]]:
        return await self._run(self._sync.detect_stale_knowledge)

    async def detect_under_connected_nodes(self) -> list[dict[str, Any]]:
        return await self._run(self._sync.detect_under_connected_nodes)

    # ------------------------------------------------------------------
    # Vector operations
    # ------------------------------------------------------------------

    async def store_embedding(
        self,
        label: str,
        key_field: str,
        key_value: str,
        embedding: list[float],
    ) -> bool:
        return await self._run(
            self._vector.store_vector_by_key,
            label, key_field, key_value, embedding,
        )

    async def search_similar(
        self,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """k-ANN search returning ``[{node_id, label, name, score}]``.

        Renames the sync vec store's ``key`` field to ``name`` so the
        shape matches :meth:`Neo4jAsyncStore.search_similar`.
        """
        rows = await self._run(
            self._vector.search_similar, query_embedding, limit,
        )
        return [
            {
                "node_id": r["node_id"],
                "label": r["label"],
                "name": r["key"],
                "score": r["score"],
            }
            for r in rows
        ]

    async def delete_embedding(self, node_id: str) -> bool:
        n = await self._run(self._vector.delete_vectors, [node_id])
        return n > 0

    async def count_embeddings(self) -> int:
        return await self._run(self._vector.count_embeddings)

    # ------------------------------------------------------------------
    # Temporal operations (DDR-003 Phase D)
    # ------------------------------------------------------------------

    async def query_at_date(
        self,
        date: str,
        label: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._sync.query_at_date, date, label, limit,
        )

    # ------------------------------------------------------------------
    # Schema / health / lifecycle
    # ------------------------------------------------------------------

    async def init_schema(
        self, cypher_statements: list[str] | None = None,
    ) -> None:
        # SQLite's schema is applied at connection time by
        # ``SqliteGraphStore.__init__``; Cypher schema statements are
        # backend-foreign and silently ignored to mirror the behaviour
        # of :meth:`SqliteGraphStore.apply_schema_statements`.
        return None

    async def health_check(self) -> dict[str, Any]:
        info = await self._run(self._sync.health_check)
        # Mirror Neo4jAsyncStore's ``{"status": "ok", "backend": ...}``
        # shape so callers can treat both backends uniformly. We keep
        # the extra sync fields (sqlite_version, path, node_count) as
        # best-effort diagnostics.
        out = {
            "status": "ok" if info.get("ok") else "error",
            "backend": "sqlite-async",
        }
        for k in ("sqlite_version", "path", "node_count"):
            if k in info:
                out[k] = info[k]
        return out

    async def close(self) -> None:
        await self._run(self._sync.close)
