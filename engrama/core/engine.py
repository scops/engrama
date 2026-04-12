"""
Engrama — Memory engine.

:class:`EngramaEngine` is the main write/read pipeline for the memory graph.
It delegates all storage operations to a ``GraphStore`` backend (see
:mod:`engrama.core.protocols`), enforcing the project's invariants:

* Every write uses ``MERGE`` — never bare ``CREATE``.
* Every node receives ``created_at`` (set once) and ``updated_at`` (refreshed).
* All Cypher uses ``$param`` parameters — no string formatting.

**Backward compatibility:** The constructor still accepts an
:class:`~engrama.core.client.EngramaClient` and wraps it in a
:class:`~engrama.backends.neo4j.backend.Neo4jGraphStore` automatically.
"""

from __future__ import annotations

from typing import Any

from neo4j import Record

from engrama.core.client import EngramaClient
from engrama.core.schema import TITLE_KEYED_LABELS


class EngramaEngine:
    """High-level read/write interface for the Engrama memory graph.

    Parameters:
        client_or_store: Either a legacy :class:`EngramaClient` (sync
            Neo4j driver wrapper) **or** a ``GraphStore`` implementation
            such as :class:`~engrama.backends.neo4j.backend.Neo4jGraphStore`
            or :class:`~engrama.backends.null.NullGraphStore`.

    When an :class:`EngramaClient` is passed, it is automatically wrapped
    in a :class:`Neo4jGraphStore` so that all internal methods use the
    protocol-based backend.  Existing code that creates an engine as
    ``EngramaEngine(client)`` continues to work unchanged.
    """

    def __init__(self, client_or_store: Any) -> None:
        if isinstance(client_or_store, EngramaClient):
            from engrama.backends.neo4j.backend import Neo4jGraphStore

            self._store = Neo4jGraphStore(client_or_store)
            self._client = client_or_store
        else:
            # Protocol-based backend (Neo4jGraphStore, NullGraphStore, …)
            self._store = client_or_store
            # Expose the underlying client if the store has one (needed by
            # reflect / recall skills that call engine._client.run()).
            self._client = getattr(client_or_store, "client", None)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def merge_node(self, label: str, properties: dict[str, Any]) -> list[Record]:
        """Create or update a node using ``MERGE``.

        The node is matched by its ``name`` property (which must be present
        in *properties*).  ``created_at`` is set only on the first write;
        ``updated_at`` is refreshed on every call.

        Parameters:
            label: The Neo4j node label (e.g. ``"Project"``).
            properties: Property dict — **must** include ``"name"``
                        (or ``"title"`` for Decision / Problem nodes).

        Returns:
            The list of result records from the query.

        Raises:
            ValueError: If *properties* contains neither ``"name"`` nor
                        ``"title"``.
        """
        # Determine the merge key — most nodes use `name`, but Decision
        # and Problem use `title` as their unique key.
        if "name" in properties:
            merge_key = "name"
        elif "title" in properties:
            merge_key = "title"
        else:
            raise ValueError(
                "properties must include 'name' or 'title' as a merge key"
            )

        merge_value = properties[merge_key]

        # Build the extra properties dict (excluding the merge key and
        # engine-managed timestamps).
        extra_props = {
            k: v
            for k, v in properties.items()
            if k not in {merge_key, "created_at", "updated_at"}
        }

        return self._store.merge_node(
            label, merge_key, merge_value, extra_props,
        )

    def merge_relation(
        self,
        from_name: str,
        from_label: str,
        rel_type: str,
        to_name: str,
        to_label: str,
    ) -> list[Record]:
        """Create or update a relationship between two existing nodes.

        Both endpoints are matched by ``name`` (or ``title`` for
        title-keyed labels).  If either node does not exist the
        relationship simply won't be created (no error).

        Parameters:
            from_name: ``name`` (or ``title``) of the source node.
            from_label: Neo4j label of the source node.
            rel_type: Relationship type (e.g. ``"USES"``).
            to_name: ``name`` (or ``title``) of the target node.
            to_label: Neo4j label of the target node.

        Returns:
            The list of result records from the query.
        """
        from_key = "title" if from_label in TITLE_KEYED_LABELS else "name"
        to_key = "title" if to_label in TITLE_KEYED_LABELS else "name"

        return self._store.merge_relation(
            from_label, from_key, from_name,
            rel_type,
            to_label, to_key, to_name,
        )

    def run(self, query: str, params: dict[str, Any] | None = None) -> list[Record]:
        """Execute a raw Cypher query (delegates to the backend).

        Prefer the higher-level methods (``merge_node``, ``merge_relation``,
        ``search``, ``get_context``) when possible.  Use ``run`` only when
        none of them cover the query you need.
        """
        return self._store.run_cypher(query, params)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[Record]:
        """Run a fulltext search against the ``memory_search`` index.

        Parameters:
            query: Lucene-syntax search string (e.g. ``"neo4j"``).
            limit: Maximum number of results to return.

        Returns:
            Records with ``type``, ``name``, and ``score`` fields.
        """
        return self._store.fulltext_search(query, limit=limit)

    def get_context(self, name: str, label: str, hops: int = 1) -> list[Record]:
        """Retrieve the local neighbourhood of a node.

        Parameters:
            name: The ``name`` property of the starting node.
            label: The Neo4j label of the starting node.
            hops: Maximum relationship depth (default ``1``).

        Returns:
            Records with ``start``, ``rel``, and ``neighbour`` fields.
        """
        # Use "name" as default key field (matches original behaviour).
        # Title-keyed nodes could be supported by checking TITLE_KEYED_LABELS
        # here, but that would change existing behaviour — left for a future PR.
        return self._store.get_neighbours(label, "name", name, hops=hops)
