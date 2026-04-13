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

        **DDR-003 Phase D** temporal fields:

        * ``valid_from`` — set on CREATE to ``datetime()`` (or the
          caller-supplied value).
        * ``confidence`` — set on CREATE to ``1.0`` (or caller-supplied).
        * ``valid_to`` — cleared on MATCH when present, signalling a
          "revived" node (conflict detection).  Callers may set it
          explicitly via *properties*.

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
        # Extract temporal fields from properties (if supplied)
        valid_from = properties.pop("valid_from", None)
        confidence = properties.pop("confidence", None)

        set_clauses_create: list[str] = [
            "n.created_at = datetime()",
            "n.updated_at = datetime()",
            f"n.valid_from = $valid_from",
            f"n.confidence = $confidence_val",
        ]
        set_clauses_match: list[str] = [
            "n.updated_at = datetime()",
        ]

        params: dict[str, Any] = {
            "merge_value": key_value,
            "valid_from": valid_from or "$$NOW$$",  # sentinel replaced below
            "confidence_val": confidence if confidence is not None else 1.0,
        }

        # Use datetime() in Cypher for valid_from when not supplied
        if valid_from is None:
            set_clauses_create[2] = "n.valid_from = datetime()"
            del params["valid_from"]
        # On MATCH: revive expired nodes by clearing valid_to
        set_clauses_match.append(
            "n.valid_to = CASE WHEN n.valid_to IS NOT NULL THEN null ELSE n.valid_to END"
        )

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

    def expire_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
    ) -> bool:
        """Set ``valid_to = datetime()`` on a node (soft expiry).

        This marks the knowledge as no longer current without deleting it.
        Re-merging the node later will clear ``valid_to`` (conflict
        detection / revival).
        """
        query = (
            f"MATCH (n:{label} {{{key_field}: $key_value}}) "
            "SET n.valid_to = datetime(), n.updated_at = datetime() "
            "RETURN n"
        )
        records = self._client.run(query, {"key_value": key_value})
        return len(records) > 0

    def decay_scores(
        self,
        rate: float = 0.01,
        min_confidence: float = 0.0,
        max_age_days: int = 0,
        label: str | None = None,
    ) -> dict[str, int]:
        """Batch-apply exponential confidence decay to all nodes.

        For each node: ``new_confidence = confidence * exp(-rate * days_old)``
        where ``days_old = (now - updated_at)`` in days.

        Args:
            rate: Exponential decay rate.
            min_confidence: Archive nodes that fall below this after decay.
            max_age_days: Archive nodes older than this many days.
            label: Optional — restrict to a single label.

        Returns:
            Dict with ``decayed`` (count updated) and ``archived``
            (count archived).
        """
        label_filter = f":{label}" if label else ""

        # Step 1: Apply decay to all nodes with confidence
        decay_query = (
            f"MATCH (n{label_filter}) "
            "WHERE n.confidence IS NOT NULL AND n.updated_at IS NOT NULL "
            "WITH n, duration.between(n.updated_at, datetime()).days AS days_old "
            "WHERE days_old > 0 "
            "SET n.confidence = n.confidence * exp(-$rate * days_old) "
            "RETURN count(n) AS decayed"
        )
        result = self._client.run(decay_query, {"rate": rate})
        decayed = result[0]["decayed"] if result else 0

        archived = 0

        # Step 2: Archive nodes below min_confidence (if threshold > 0)
        if min_confidence > 0:
            archive_query = (
                f"MATCH (n{label_filter}) "
                "WHERE n.confidence IS NOT NULL AND n.confidence < $min_conf "
                "AND (n.status IS NULL OR n.status <> 'archived') "
                "SET n.status = 'archived', n.updated_at = datetime() "
                "RETURN count(n) AS archived"
            )
            result = self._client.run(archive_query, {"min_conf": min_confidence})
            archived += result[0]["archived"] if result else 0

        # Step 3: Archive nodes older than max_age_days (if set)
        if max_age_days > 0:
            age_query = (
                f"MATCH (n{label_filter}) "
                "WHERE n.updated_at IS NOT NULL "
                "AND duration.between(n.updated_at, datetime()).days > $max_age "
                "AND (n.status IS NULL OR n.status <> 'archived') "
                "SET n.status = 'archived', n.updated_at = datetime() "
                "RETURN count(n) AS archived"
            )
            result = self._client.run(age_query, {"max_age": max_age_days})
            archived += result[0]["archived"] if result else 0

        return {"decayed": decayed, "archived": archived}

    def query_at_date(
        self,
        date: str,
        label: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Query what was true at a specific date.

        Returns nodes where ``valid_from <= date`` and
        ``valid_to IS NULL OR valid_to >= date``.

        Args:
            date: ISO-format date string (e.g. ``"2026-01-15"``).
            label: Optional label filter.
            limit: Maximum results.

        Returns:
            List of dicts with label, name, confidence, valid_from,
            valid_to, and status.
        """
        label_clause = f":{label}" if label else ""
        query = (
            f"MATCH (n{label_clause}) "
            "WHERE n.valid_from IS NOT NULL "
            "  AND n.valid_from <= datetime($date) "
            "  AND (n.valid_to IS NULL OR n.valid_to >= datetime($date)) "
            "  AND NOT n:Insight AND NOT n:Domain "
            "RETURN labels(n)[0] AS label, "
            "  COALESCE(n.name, n.title) AS name, "
            "  n.confidence AS confidence, "
            "  n.valid_from AS valid_from, "
            "  n.valid_to AS valid_to, "
            "  n.status AS status "
            "ORDER BY n.confidence DESC "
            "LIMIT $limit"
        )
        return self._client.run(query, {"date": date, "limit": limit})

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

        Returns records with ``type``, ``name``, ``score``, and temporal
        fields (``confidence``, ``updated_at``) for Phase D scoring.
        """
        cypher = (
            'CALL db.index.fulltext.queryNodes("memory_search", $query) '
            "YIELD node, score "
            "RETURN labels(node)[0] AS type, "
            "COALESCE(node.name, node.title) AS name, "
            "score, "
            "node.confidence AS confidence, "
            "node.updated_at AS updated_at "
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

    def count_labels(self) -> dict[str, int]:
        """Count nodes per label.  Used by reflect to profile the graph."""
        records = self._client.run(
            "MATCH (n) WHERE NOT n:Insight "
            "RETURN labels(n)[0] AS label, count(n) AS cnt "
            "ORDER BY cnt DESC",
        )
        return {r["label"]: r["cnt"] for r in records}

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

    def close(self) -> None:
        """Close the underlying client connection."""
        self._client.close()
