"""
Engrama — Neo4j vector store.

Implements the ``VectorStore`` protocol using Neo4j's native vector index
(available since Neo4j 5.11).

Strategy: a secondary ``:Embedded`` label is added to every node that
carries an embedding.  A single vector index on ``(:Embedded)`` covers
all primary labels, so one ``db.index.vector.queryNodes`` call searches
everything — no per-label indexes needed.

Configuration::

    VECTOR_BACKEND=neo4j
    EMBEDDING_DIMENSIONS=768      # must match the embedding model

The vector index (``memory_vectors``) is created by ``init-schema.cypher``
or by :meth:`ensure_index`.
"""

from __future__ import annotations

import logging
from typing import Any

from engrama.core.client import EngramaClient
from engrama.core.scope import MemoryScope, scope_filter_cypher

logger = logging.getLogger("engrama.backends.neo4j.vector")


class Neo4jVectorStore:
    """Sync ``VectorStore`` implementation backed by Neo4j's native vector index.

    Parameters:
        client: An initialised :class:`EngramaClient`.
        dimensions: Embedding dimensionality (e.g. 768 for nomic-embed-text).
        index_name: Name of the Neo4j vector index.  Defaults to
            ``"memory_vectors"``.
    """

    def __init__(
        self,
        client: EngramaClient,
        dimensions: int = 768,
        index_name: str = "memory_vectors",
    ) -> None:
        self._client = client
        self.dimensions: int = dimensions
        self._index_name: str = index_name

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------

    def store_vectors(
        self,
        items: list[tuple[str, list[float]]],
    ) -> int:
        """Store embeddings on existing nodes and add the ``:Embedded`` label.

        Args:
            items: ``[(node_element_id, embedding)]`` pairs.  The element
                ID is Neo4j's internal ``elementId(n)``.

        Returns:
            Count of nodes updated.
        """
        if not items:
            return 0

        stored = 0
        for element_id, embedding in items:
            try:
                records = self._client.run(
                    "MATCH (n) WHERE elementId(n) = $eid "
                    "SET n.embedding = $embedding, n:Embedded "
                    "RETURN elementId(n) AS eid",
                    {"eid": element_id, "embedding": embedding},
                )
                if records:
                    stored += 1
            except Exception as e:
                logger.warning("Failed to store vector for %s: %s", element_id, e)
        return stored

    def store_vector_by_key(
        self,
        label: str,
        key_field: str,
        key_value: str,
        embedding: list[float],
    ) -> bool:
        """Store an embedding on a node identified by label + key.

        This is a convenience method used by the engine's embed-on-write
        path, where we know the label and merge key but not the elementId.

        Returns:
            ``True`` if the node was found and updated.
        """
        records = self._client.run(
            f"MATCH (n:{label} {{{key_field}: $key_value}}) "
            "SET n.embedding = $embedding, n:Embedded "
            "RETURN elementId(n) AS eid",
            {"key_value": key_value, "embedding": embedding},
        )
        return len(records) > 0

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_vectors(
        self,
        query_embedding: list[float],
        limit: int = 10,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """k-ANN similarity search using the Neo4j vector index.

        Returns:
            List of dicts with ``node_id`` (elementId), ``label``,
            ``name``, ``score`` and ``trust_level``.

        DDR-003 Phase F: when ``scope`` is set, hits are filtered by the
        scope-visibility rule (see :mod:`engrama.core.scope`).
        """
        scope_clause, scope_params = scope_filter_cypher(scope, "node")
        where_sql = f"WHERE {scope_clause} " if scope_clause else ""
        cypher = (
            f"CALL db.index.vector.queryNodes('{self._index_name}', $k, $embedding) "
            "YIELD node, score "
            f"{where_sql}"
            "WITH node, score, "
            "[l IN labels(node) WHERE l <> 'Embedded'][0] AS primary_label "
            "RETURN elementId(node) AS node_id, "
            "primary_label AS label, "
            "COALESCE(node.name, node.title) AS name, "
            "score, "
            "node.trust_level AS trust_level "
            "ORDER BY score DESC "
            "LIMIT $limit"
        )
        params: dict[str, Any] = {
            "k": limit,
            "embedding": query_embedding,
            "limit": limit,
            **scope_params,
        }
        records = self._client.run(cypher, params)
        return [dict(r) for r in records]

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_vectors(
        self,
        node_ids: list[str],
    ) -> int:
        """Remove embeddings and the ``:Embedded`` label from nodes.

        Args:
            node_ids: List of Neo4j elementId strings.

        Returns:
            Count of nodes updated.
        """
        if not node_ids:
            return 0

        records = self._client.run(
            "UNWIND $ids AS eid "
            "MATCH (n) WHERE elementId(n) = eid "
            "REMOVE n.embedding, n:Embedded "
            "RETURN count(n) AS removed",
            {"ids": node_ids},
        )
        return records[0]["removed"] if records else 0

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Return the total number of nodes with embeddings."""
        records = self._client.run("MATCH (n:Embedded) RETURN count(n) AS total")
        return records[0]["total"] if records else 0

    # ------------------------------------------------------------------
    # Migration helpers (iter_all_vectors, purge_all) — used by engrama
    # export / import. Not on the VectorStore protocol because they
    # only make sense for the bulk-migration code path.
    # ------------------------------------------------------------------

    def iter_all_vectors(self):
        """Yield ``{label, key_field, key_value, vector}`` for every
        ``:Embedded`` node, resolved to the primary label + merge key so
        the dump is portable across backends.
        """
        records = self._client.run(
            "MATCH (n:Embedded) "
            "WITH n, [l IN labels(n) WHERE l <> 'Embedded'][0] AS label "
            "WHERE label IS NOT NULL "
            "RETURN label, "
            "       n.name      AS name, "
            "       n.title     AS title, "
            "       n.embedding AS embedding"
        )
        for r in records:
            name = r["name"]
            title = r["title"]
            if name:
                key_field, key_value = "name", name
            elif title:
                key_field, key_value = "title", title
            else:
                continue
            yield {
                "label": r["label"],
                "key_field": key_field,
                "key_value": key_value,
                "vector": list(r["embedding"] or []),
            }

    def purge_all(self) -> None:
        """No-op: vectors live on nodes, so wiping nodes (the graph
        store's ``purge_all``) already drops every embedding. The method
        exists for symmetry with :class:`SqliteVecStore`.
        """
        return None

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def ensure_index(self) -> None:
        """Create the vector index if it doesn't exist.

        This is idempotent — safe to call on every startup.  The
        preferred path is ``init-schema.cypher``, but this method
        provides a programmatic fallback.
        """
        try:
            self._client.run(
                f"CREATE VECTOR INDEX {self._index_name} IF NOT EXISTS "
                "FOR (n:Embedded) ON (n.embedding) "
                "OPTIONS {indexConfig: {"
                f"`vector.dimensions`: {self.dimensions}, "
                "`vector.similarity_function`: 'cosine'"
                "}}"
            )
            logger.info(
                "Vector index '%s' ensured (dimensions=%d)",
                self._index_name,
                self.dimensions,
            )
        except Exception as e:
            logger.warning("Could not create vector index: %s", e)

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"Neo4jVectorStore(dimensions={self.dimensions}, index={self._index_name!r})"
