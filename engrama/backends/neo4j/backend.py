"""
Engrama — Neo4j graph store.

Implements the ``GraphStore`` protocol using Neo4j's sync driver.
This module contains the **exact same Cypher** that previously lived in
``core/engine.py`` — it was extracted, not rewritten.

The class also exposes the underlying :class:`EngramaClient` via the
``client`` property so that callers that need raw ``list[Record]`` results
(e.g. the reflect skill's Cypher queries) can still use it directly.
"""

from __future__ import annotations

from typing import Any

from neo4j import Record

from engrama.core.client import EngramaClient
from engrama.core.schema import TITLE_KEYED_LABELS


class Neo4jGraphStore:
    """Sync ``GraphStore`` implementation backed by Neo4j.

    Wraps an :class:`EngramaClient` and exposes the same Cypher that
    ``EngramaEngine`` used to run inline.

    Parameters:
        client: An initialised and verified :class:`EngramaClient`.
    """

    def __init__(self, client: EngramaClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def client(self) -> EngramaClient:
        """Direct access to the underlying sync driver wrapper.

        Skills and adapters that need raw ``list[Record]`` results (e.g.
        the reflect skill) can use ``store.client.run(cypher, params)``.
        """
        return self._client

    # ------------------------------------------------------------------
    # Node operations
    # ------------------------------------------------------------------

    def merge_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        properties: dict[str, Any],
        embedding: list[float] | None = None,
    ) -> list[Record]:
        """Create or update a node using ``MERGE``.

        ``created_at`` is set only on the first write; ``updated_at`` is
        refreshed on every call.

        Parameters:
            label: The Neo4j node label (e.g. ``"Project"``).
            key_field: The merge key property (``"name"`` or ``"title"``).
            key_value: The value of the merge key.
            properties: Extra properties to set (must **not** include the
                merge key or timestamps).
            embedding: Optional embedding vector (stored as a property
                for future vector index usage).

        Returns:
            A ``list[Record]`` with the merged node.
        """
        set_clauses_create: list[str] = [
            "n.created_at = datetime()",
            "n.updated_at = datetime()",
        ]
        set_clauses_match: list[str] = [
            "n.updated_at = datetime()",
        ]

        params: dict[str, Any] = {"merge_value": key_value}

        for idx, (key, value) in enumerate(properties.items()):
            param_name = f"p{idx}"
            set_clauses_create.append(f"n.{key} = ${param_name}")
            set_clauses_match.append(f"n.{key} = ${param_name}")
            params[param_name] = value

        if embedding is not None:
            set_clauses_create.append("n.embedding = $embedding")
            set_clauses_match.append("n.embedding = $embedding")
            params["embedding"] = embedding

        on_create = ", ".join(set_clauses_create)
        on_match = ", ".join(set_clauses_match)

        query = (
            f"MERGE (n:{label} {{{key_field}: $merge_value}}) "
            f"ON CREATE SET {on_create} "
            f"ON MATCH SET {on_match} "
            "RETURN n"
        )

        return self._client.run(query, params)

    def get_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
    ) -> dict[str, Any] | None:
        """Retrieve a single node by its unique key."""
        query = (
            f"MATCH (n:{label} {{{key_field}: $key_value}}) "
            "RETURN n"
        )
        records = self._client.run(query, {"key_value": key_value})
        if records:
            return dict(records[0]["n"])
        return None

    def delete_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        soft: bool = True,
    ) -> bool:
        """Delete or archive a node.

        When ``soft=True``, sets ``status='archived'`` and ``updated_at``.
        When ``soft=False``, detach-deletes the node.
        """
        if soft:
            query = (
                f"MATCH (n:{label} {{{key_field}: $key_value}}) "
                "SET n.status = 'archived', n.updated_at = datetime() "
                "RETURN n"
            )
        else:
            query = (
                f"MATCH (n:{label} {{{key_field}: $key_value}}) "
                "DETACH DELETE n "
                "RETURN true AS deleted"
            )
        records = self._client.run(query, {"key_value": key_value})
        return len(records) > 0

    # ------------------------------------------------------------------
    # Relationship operations
    # ------------------------------------------------------------------

    def merge_relation(
        self,
        from_label: str,
        from_key: str,
        from_value: str,
        rel_type: str,
        to_label: str,
        to_key: str,
        to_value: str,
    ) -> list[Record]:
        """Create a relationship between two existing nodes (idempotent).

        If either endpoint does not exist, the relationship simply won't
        be created (no error).
        """
        query = (
            f"MATCH (a:{from_label} {{{from_key}: $from_value}}) "
            f"MATCH (b:{to_label} {{{to_key}: $to_value}}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "RETURN type(r) AS rel_type"
        )
        params = {"from_value": from_value, "to_value": to_value}
        return self._client.run(query, params)

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def get_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
        limit: int = 50,
    ) -> list[Record]:
        """Traverse N hops from a node and return its neighbourhood."""
        query = (
            f"MATCH (start:{label} {{{key_field}: $key_value}})"
            f"-[rel*1..{hops}]-(neighbour) "
            "RETURN start, rel, neighbour"
        )
        return self._client.run(query, {"key_value": key_value})

    def fulltext_search(
        self,
        query: str,
        limit: int = 10,
    ) -> list[Record]:
        """Keyword search against the ``memory_search`` fulltext index.

        Returns records with ``type``, ``name``, and ``score`` fields.
        """
        cypher = (
            'CALL db.index.fulltext.queryNodes("memory_search", $query) '
            "YIELD node, score "
            "RETURN labels(node)[0] AS type, node.name AS name, score "
            "ORDER BY score DESC LIMIT $limit"
        )
        return self._client.run(cypher, {"query": query, "limit": limit})

    def run_cypher(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[Record]:
        """Execute a raw Cypher query.

        Delegates directly to :meth:`EngramaClient.run`.
        """
        return self._client.run(query, params)

    # ------------------------------------------------------------------
    # Schema operations
    # ------------------------------------------------------------------

    def init_schema(self, schema: Any = None) -> None:
        """Apply constraints and indexes.

        For Phase A this is a no-op — schema is managed by
        ``scripts/init-schema.cypher``.
        """
        pass

    def health_check(self) -> dict[str, Any]:
        """Verify Neo4j connectivity and return status info."""
        self._client.verify()
        return {
            "status": "ok",
            "backend": "neo4j",
            "uri": self._client._uri,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release the underlying driver and its connection pool."""
        self._client.close()

    def __repr__(self) -> str:
        return f"Neo4jGraphStore(client={self._client!r})"
