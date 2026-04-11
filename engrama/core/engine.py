"""
Engrama — Memory engine.

:class:`EngramaEngine` is the main write/read pipeline for the memory graph.
It wraps an :class:`~engrama.core.client.EngramaClient` and enforces the
project's invariants:

* Every write uses ``MERGE`` — never bare ``CREATE``.
* Every node receives ``created_at`` (set once) and ``updated_at`` (refreshed).
* All Cypher uses ``$param`` parameters — no string formatting.
"""

from __future__ import annotations

from typing import Any

from neo4j import Record

from engrama.core.client import EngramaClient


class EngramaEngine:
    """High-level read/write interface for the Engrama memory graph.

    Parameters:
        client: An initialised :class:`EngramaClient` connected to Neo4j.
    """

    def __init__(self, client: EngramaClient) -> None:
        self._client = client

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

        # Build the SET clause for all remaining properties (excluding the
        # merge key and the engine-managed timestamps).
        extra_props = {
            k: v
            for k, v in properties.items()
            if k not in {merge_key, "created_at", "updated_at"}
        }

        # Cypher template:
        #   MERGE (n:Label {name: $merge_value})
        #   ON CREATE SET n.created_at = datetime(), n.updated_at = datetime(), ...
        #   ON MATCH  SET n.updated_at = datetime(), ...
        #   RETURN n
        set_clauses_create: list[str] = [
            "n.created_at = datetime()",
            "n.updated_at = datetime()",
        ]
        set_clauses_match: list[str] = [
            "n.updated_at = datetime()",
        ]

        params: dict[str, Any] = {"merge_value": merge_value}

        for idx, (key, value) in enumerate(extra_props.items()):
            param_name = f"p{idx}"
            set_clauses_create.append(f"n.{key} = ${param_name}")
            set_clauses_match.append(f"n.{key} = ${param_name}")
            params[param_name] = value

        on_create = ", ".join(set_clauses_create)
        on_match = ", ".join(set_clauses_match)

        query = (
            f"MERGE (n:{label} {{{merge_key}: $merge_value}}) "
            f"ON CREATE SET {on_create} "
            f"ON MATCH SET {on_match} "
            "RETURN n"
        )

        return self._client.run(query, params)

    def merge_relation(
        self,
        from_name: str,
        from_label: str,
        rel_type: str,
        to_name: str,
        to_label: str,
    ) -> list[Record]:
        """Create or update a relationship between two existing nodes.

        Both endpoints are matched by ``name``.  If either node does not
        exist the relationship simply won't be created (no error).

        Parameters:
            from_name: ``name`` property of the source node.
            from_label: Neo4j label of the source node.
            rel_type: Relationship type (e.g. ``"USES"``).
            to_name: ``name`` property of the target node.
            to_label: Neo4j label of the target node.

        Returns:
            The list of result records from the query.
        """
        query = (
            f"MATCH (a:{from_label} {{name: $from_name}}) "
            f"MATCH (b:{to_label} {{name: $to_name}}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "RETURN type(r) AS rel_type"
        )
        params = {"from_name": from_name, "to_name": to_name}
        return self._client.run(query, params)

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
        cypher = (
            'CALL db.index.fulltext.queryNodes("memory_search", $query) '
            "YIELD node, score "
            "RETURN labels(node)[0] AS type, node.name AS name, score "
            "ORDER BY score DESC LIMIT $limit"
        )
        return self._client.run(cypher, {"query": query, "limit": limit})

    def get_context(self, name: str, label: str, hops: int = 1) -> list[Record]:
        """Retrieve the local neighbourhood of a node.

        Parameters:
            name: The ``name`` property of the starting node.
            label: The Neo4j label of the starting node.
            hops: Maximum relationship depth (default ``1``).

        Returns:
            Records with ``start``, ``rel``, and ``neighbour`` fields.
        """
        cypher = (
            f"MATCH (start:{label} {{name: $name}})-[rel*1..{hops}]-(neighbour) "
            "RETURN start, rel, neighbour"
        )
        return self._client.run(cypher, {"name": name})
