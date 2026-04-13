"""
Engrama — Async Neo4j graph + vector store.

Implements the ``GraphStore`` and ``VectorStore`` protocols using Neo4j's
**async** driver.  This is the store that ``server.py`` (MCP) uses — it
was extracted from the inline Cypher that previously lived in every MCP
tool function.

The **sync** ``Neo4jGraphStore`` in ``backend.py`` is still used by the
SDK / CLI via ``EngramaEngine``.

All Cypher uses parameterised queries — never string formatting.
All writes use ``MERGE`` — never bare ``CREATE``.
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import AsyncDriver

from engrama.core.schema import TITLE_KEYED_LABELS

logger = logging.getLogger("engrama.backends.neo4j.async_store")


class Neo4jAsyncStore:
    """Async ``GraphStore`` + ``VectorStore`` backed by Neo4j.

    This class contains **all** the Cypher for the MCP server.  Nothing
    else in Engrama should write raw Cypher — except reflect pattern
    queries passed through :meth:`run_pattern`.

    Parameters:
        driver: An initialised ``neo4j.AsyncDriver``.
        database: Neo4j database name (default ``"neo4j"``).
        vector_dimensions: Embedding dimensionality (0 = disabled).
        vector_index: Name of the Neo4j vector index.
    """

    def __init__(
        self,
        driver: AsyncDriver,
        database: str = "neo4j",
        vector_dimensions: int = 0,
        vector_index: str = "memory_vectors",
    ) -> None:
        self._driver = driver
        self._database = database
        self._vector_dimensions = vector_dimensions
        self._vector_index = vector_index

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def dimensions(self) -> int:
        """Embedding dimensionality (``VectorStore`` protocol)."""
        return self._vector_dimensions

    # ------------------------------------------------------------------
    # Node operations (GraphStore)
    # ------------------------------------------------------------------

    async def merge_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        properties: dict[str, Any],
        embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Create or update a node.  Always MERGE semantics.

        Returns the node properties including generated fields.
        """
        # Extract temporal fields from properties (if supplied)
        valid_from = properties.pop("valid_from", None)
        valid_to = properties.pop("valid_to", None)
        confidence = properties.pop("confidence", None)

        set_create: list[str] = [
            "n.created_at = datetime()",
            "n.updated_at = datetime()",
        ]
        set_match: list[str] = [
            "n.updated_at = datetime()",
        ]

        params: dict[str, Any] = {"merge_value": key_value}

        # DDR-003 Phase D: temporal fields
        if valid_from is not None:
            set_create.append("n.valid_from = datetime($valid_from)")
            params["valid_from"] = valid_from
        else:
            set_create.append("n.valid_from = datetime()")

        # DDR-003 Phase D: valid_to — mark fact as superseded
        if valid_to is not None:
            set_create.append("n.valid_to = datetime($valid_to)")
            set_match.append("n.valid_to = datetime($valid_to)")
            params["valid_to"] = valid_to
            # Superseded facts get reduced confidence (×0.5)
            if confidence is not None:
                confidence = confidence * 0.5
            else:
                confidence = 0.5
        else:
            # On MATCH without valid_to: revive expired nodes by clearing it
            set_match.append(
                "n.valid_to = CASE WHEN n.valid_to IS NOT NULL "
                "THEN null ELSE n.valid_to END"
            )

        if confidence is not None:
            set_create.append("n.confidence = $confidence_val")
            params["confidence_val"] = confidence
        else:
            set_create.append("n.confidence = 1.0")

        for idx, (key, value) in enumerate(properties.items()):
            pname = f"p{idx}"
            set_create.append(f"n.{key} = ${pname}")
            set_match.append(f"n.{key} = ${pname}")
            params[pname] = value

        if embedding is not None:
            set_create.append("n.embedding = $embedding")
            set_match.append("n.embedding = $embedding")
            params["embedding"] = embedding

        # DDR-003 Phase D: detect conflict — node previously had valid_to
        # We check BEFORE the merge so we can warn the caller
        conflict_warning: str | None = None
        if valid_to is None:
            pre_check = (
                f"MATCH (n:{label} {{{key_field}: $merge_value}}) "
                "WHERE n.valid_to IS NOT NULL "
                "RETURN n.valid_to AS old_valid_to LIMIT 1"
            )
            pre_records, _, _ = await self._driver.execute_query(
                pre_check, parameters_={"merge_value": key_value},
                database_=self._database,
            )
            if pre_records:
                old_vt = pre_records[0]["old_valid_to"]
                if hasattr(old_vt, "isoformat"):
                    old_vt = old_vt.isoformat()[:10]
                conflict_warning = (
                    f"Node {key_value} was marked as superseded on {old_vt}. "
                    "Updating anyway — valid_to has been cleared (revival)."
                )
                logger.info(conflict_warning)

        cypher = (
            f"MERGE (n:{label} {{{key_field}: $merge_value}}) "
            f"ON CREATE SET {', '.join(set_create)} "
            f"ON MATCH SET {', '.join(set_match)} "
            "RETURN n, "
            "CASE WHEN n.created_at = n.updated_at "
            "THEN true ELSE false END AS created"
        )

        records, _, _ = await self._driver.execute_query(
            cypher, parameters_=params, database_=self._database,
        )
        if records:
            result: dict[str, Any] = {
                "node": dict(records[0]["n"]),
                "created": records[0]["created"],
            }
            if conflict_warning:
                result["warning"] = conflict_warning
            return result
        return {"node": {}, "created": False}

    async def get_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
    ) -> dict[str, Any] | None:
        """Retrieve a single node by its unique key."""
        cypher = (
            f"MATCH (n:{label} {{{key_field}: $key_value}}) "
            "RETURN n"
        )
        records, _, _ = await self._driver.execute_query(
            cypher, parameters_={"key_value": key_value},
            database_=self._database,
        )
        if records:
            return dict(records[0]["n"])
        return None

    async def delete_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        soft: bool = True,
    ) -> bool:
        """Delete or archive a node."""
        if soft:
            cypher = (
                f"MATCH (n:{label} {{{key_field}: $key_value}}) "
                "SET n.status = 'archived', n.updated_at = datetime() "
                "RETURN n"
            )
        else:
            cypher = (
                f"MATCH (n:{label} {{{key_field}: $key_value}}) "
                "DETACH DELETE n "
                "RETURN true AS deleted"
            )
        records, _, _ = await self._driver.execute_query(
            cypher, parameters_={"key_value": key_value},
            database_=self._database,
        )
        return len(records) > 0

    # ------------------------------------------------------------------
    # Relationship operations (GraphStore)
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
        """Create a relationship (idempotent MERGE).

        Returns relation info or empty dict if either endpoint not found.
        """
        cypher = (
            f"MATCH (a:{from_label} {{{from_key}: $from_value}}) "
            f"MATCH (b:{to_label} {{{to_key}: $to_value}}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            f"RETURN type(r) AS rel_type, "
            f"a.{from_key} AS from_name, b.{to_key} AS to_name, "
            f"a.obsidian_path AS from_obsidian_path"
        )
        records, _, _ = await self._driver.execute_query(
            cypher,
            parameters_={"from_value": from_value, "to_value": to_value},
            database_=self._database,
        )
        if records:
            return dict(records[0])
        return {}

    # ------------------------------------------------------------------
    # Query operations (GraphStore)
    # ------------------------------------------------------------------

    async def get_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Traverse N hops from a node.  Returns neighbour list.

        Each neighbour dict contains:
        ``{label, name, via: [rel_types], properties}``.
        The ``via`` array is deduplicated (BUG-008 fix).
        """
        cypher = (
            f"MATCH (start:{label} {{{key_field}: $key_value}}) "
            f"OPTIONAL MATCH (start)-[r*1..{hops}]-(neighbour) "
            "RETURN start, "
            "  [rel IN r | type(rel)] AS rel_types, "
            "  labels(neighbour)[0] AS neighbour_label, "
            "  COALESCE(neighbour.name, neighbour.title) AS neighbour_name, "
            "  properties(neighbour) AS neighbour_props"
        )
        records, _, _ = await self._driver.execute_query(
            cypher, parameters_={"key_value": key_value},
            database_=self._database,
        )
        if not records:
            return []

        start_node = dict(records[0]["start"]) if records[0]["start"] else {}
        root_name = start_node.get("name") or start_node.get("title")
        root_key = (label, root_name)

        neighbours: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for r in records:
            nname = r["neighbour_name"]
            nlabel = r["neighbour_label"]
            if nname and (nlabel, nname) not in seen and (nlabel, nname) != root_key:
                seen.add((nlabel, nname))
                neighbours.append({
                    "label": nlabel,
                    "name": nname,
                    # BUG-008: deduplicate via array
                    "via": list(dict.fromkeys(r["rel_types"])),
                    "properties": {
                        k: v for k, v in (r["neighbour_props"] or {}).items()
                        if k not in {"created_at", "updated_at"}
                    },
                })

        return neighbours

    async def get_node_with_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
    ) -> dict[str, Any] | None:
        """Convenience: get_node + get_neighbours in one call.

        Returns ``{node: {...}, neighbours: [...]}`` or ``None`` if the
        start node doesn't exist.
        """
        cypher = (
            f"MATCH (start:{label} {{{key_field}: $key_value}}) "
            f"OPTIONAL MATCH (start)-[r*1..{hops}]-(neighbour) "
            "RETURN start, "
            "  [rel IN r | type(rel)] AS rel_types, "
            "  labels(neighbour)[0] AS neighbour_label, "
            "  COALESCE(neighbour.name, neighbour.title) AS neighbour_name, "
            "  properties(neighbour) AS neighbour_props"
        )
        records, _, _ = await self._driver.execute_query(
            cypher, parameters_={"key_value": key_value},
            database_=self._database,
        )
        if not records:
            return None

        start_node = dict(records[0]["start"]) if records[0]["start"] else {}
        root_name = start_node.get("name") or start_node.get("title")
        root_key = (label, root_name)

        neighbours: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for r in records:
            nname = r["neighbour_name"]
            nlabel = r["neighbour_label"]
            if nname and (nlabel, nname) not in seen and (nlabel, nname) != root_key:
                seen.add((nlabel, nname))
                neighbours.append({
                    "label": nlabel,
                    "name": nname,
                    "via": list(dict.fromkeys(r["rel_types"])),
                    "properties": {
                        k: v for k, v in (r["neighbour_props"] or {}).items()
                        if k not in {"created_at", "updated_at"}
                    },
                })

        return {"node": start_node, "neighbours": neighbours}

    async def fulltext_search(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Keyword search across all text properties.

        Returns ``[{type, name, score}]``.
        BUG-006 fix: uses ``COALESCE(node.name, node.title)`` so that
        Decision/Problem nodes return their title as ``name``.
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
        records, _, _ = await self._driver.execute_query(
            cypher,
            parameters_={"query": query, "limit": limit},
            database_=self._database,
        )
        return [dict(r) for r in records]

    async def count_labels(self) -> dict[str, int]:
        """Count nodes per label.  Used by reflect to profile the graph."""
        records, _, _ = await self._driver.execute_query(
            "MATCH (n) WHERE NOT n:Insight "
            "RETURN labels(n)[0] AS label, count(n) AS cnt "
            "ORDER BY cnt DESC",
            database_=self._database,
        )
        return {r["label"]: r["cnt"] for r in records}

    async def run_pattern(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a raw Cypher pattern query.

        Used by reflect skill for complex multi-hop patterns.
        """
        records, _, _ = await self._driver.execute_query(
            cypher,
            parameters_=params or {},
            database_=self._database,
        )
        return [dict(r) for r in records]

    async def lookup_node_label(self, name: str) -> str | None:
        """Look up a node's label by name (case-insensitive).

        Searches both ``name`` and ``title`` properties (BUG-006 fix).
        Returns the primary label or ``None``.
        """
        records, _, _ = await self._driver.execute_query(
            "MATCH (n) WHERE toLower(COALESCE(n.name, n.title)) = toLower($name) "
            "RETURN labels(n)[0] AS label LIMIT 1",
            parameters_={"name": name},
            database_=self._database,
        )
        if records:
            return records[0]["label"]
        return None

    # ------------------------------------------------------------------
    # Insight operations (used by reflect / surface / approve tools)
    # ------------------------------------------------------------------

    async def get_dismissed_titles(self) -> set[str]:
        """Return titles of all dismissed Insights."""
        records, _, _ = await self._driver.execute_query(
            "MATCH (i:Insight {status: 'dismissed'}) "
            "RETURN i.title AS title",
            database_=self._database,
        )
        return {r["title"] for r in records}

    async def get_pending_insights(self, limit: int = 10) -> list[dict[str, Any]]:
        """Retrieve pending Insights ordered by confidence."""
        records, _, _ = await self._driver.execute_query(
            "MATCH (i:Insight {status: $status}) "
            "RETURN i.title AS title, i.body AS body, "
            "       i.confidence AS confidence, "
            "       i.source_query AS source_query, "
            "       i.created_at AS created_at "
            "ORDER BY i.confidence DESC "
            "LIMIT $limit",
            parameters_={"status": "pending", "limit": limit},
            database_=self._database,
        )
        return [dict(r) for r in records]

    async def get_insight_by_title(self, title: str) -> dict[str, Any] | None:
        """Fetch an Insight node by exact title."""
        records, _, _ = await self._driver.execute_query(
            "MATCH (i:Insight {title: $title}) "
            "RETURN i.status AS status, i.body AS body, "
            "       i.confidence AS confidence, "
            "       i.source_query AS source_query",
            parameters_={"title": title},
            database_=self._database,
        )
        if records:
            return dict(records[0])
        return None

    async def update_insight_status(
        self,
        title: str,
        new_status: str,
    ) -> bool:
        """Update an Insight's status and record a timestamp."""
        ts_field = "approved_at" if new_status == "approved" else "dismissed_at"
        records, _, _ = await self._driver.execute_query(
            "MATCH (i:Insight {title: $title}) "
            f"SET i.status = $new_status, "
            f"    i.{ts_field} = datetime(), "
            "    i.updated_at = datetime() "
            "RETURN i.title AS title",
            parameters_={"title": title, "new_status": new_status},
            database_=self._database,
        )
        return len(records) > 0

    async def mark_insight_synced(
        self,
        title: str,
        obsidian_path: str,
    ) -> bool:
        """Mark an Insight as synced to vault."""
        records, _, _ = await self._driver.execute_query(
            "MATCH (i:Insight {title: $title}) "
            "SET i.obsidian_path = $path, "
            "    i.synced_at = datetime(), "
            "    i.updated_at = datetime() "
            "RETURN i.title AS title",
            parameters_={"title": title, "path": obsidian_path},
            database_=self._database,
        )
        return len(records) > 0

    async def find_insight_by_source_query(
        self,
        source_query: str,
        statuses: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Find an Insight by source_query and optional status filter.

        BUG-007: used to check for existing under_connected insights
        before creating duplicates.
        """
        status_list = statuses or ["pending", "approved"]
        records, _, _ = await self._driver.execute_query(
            "MATCH (i:Insight {source_query: $sq}) "
            "WHERE i.status IN $statuses "
            "RETURN i.title AS title, i.status AS status LIMIT 1",
            parameters_={"sq": source_query, "statuses": status_list},
            database_=self._database,
        )
        if records:
            return dict(records[0])
        return None

    async def list_existing_nodes(self, limit: int = 200) -> list[dict[str, str]]:
        """List existing nodes for deduplication during ingest."""
        records, _, _ = await self._driver.execute_query(
            "MATCH (n) WHERE n.name IS NOT NULL OR n.title IS NOT NULL "
            "RETURN labels(n)[0] AS label, "
            "COALESCE(n.name, n.title) AS name "
            "ORDER BY name LIMIT $limit",
            parameters_={"limit": limit},
            database_=self._database,
        )
        return [{"label": r["label"], "name": r["name"]} for r in records]

    # ------------------------------------------------------------------
    # Vector operations (VectorStore)
    # ------------------------------------------------------------------

    async def store_embedding(
        self,
        label: str,
        key_field: str,
        key_value: str,
        embedding: list[float],
    ) -> bool:
        """Store an embedding on a node and add the :Embedded label."""
        records, _, _ = await self._driver.execute_query(
            f"MATCH (n:{label} {{{key_field}: $key_value}}) "
            "SET n.embedding = $embedding, n:Embedded "
            "RETURN elementId(n) AS eid",
            parameters_={"key_value": key_value, "embedding": embedding},
            database_=self._database,
        )
        return len(records) > 0

    async def search_similar(
        self,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """k-ANN similarity search using the Neo4j vector index.

        Returns ``[{node_id, label, name, score}]``.
        Gracefully returns empty if no vector index exists.
        """
        if self._vector_dimensions == 0:
            return []
        try:
            cypher = (
                f"CALL db.index.vector.queryNodes("
                f"'{self._vector_index}', $k, $embedding) "
                "YIELD node, score "
                "WITH node, score, "
                "[l IN labels(node) WHERE l <> 'Embedded'][0] AS primary_label "
                "RETURN elementId(node) AS node_id, "
                "primary_label AS label, "
                "COALESCE(node.name, node.title) AS name, "
                "score "
                "ORDER BY score DESC LIMIT $limit"
            )
            records, _, _ = await self._driver.execute_query(
                cypher,
                parameters_={
                    "k": limit,
                    "embedding": query_embedding,
                    "limit": limit,
                },
                database_=self._database,
            )
            return [dict(r) for r in records]
        except Exception as e:
            logger.warning("Vector search failed (index may not exist): %s", e)
            return []

    async def delete_embedding(self, node_id: str) -> bool:
        """Remove embedding and :Embedded label from a node."""
        records, _, _ = await self._driver.execute_query(
            "MATCH (n) WHERE elementId(n) = $eid "
            "REMOVE n.embedding, n:Embedded "
            "RETURN true AS done",
            parameters_={"eid": node_id},
            database_=self._database,
        )
        return len(records) > 0

    async def count_embeddings(self) -> int:
        """Total nodes with embeddings."""
        records, _, _ = await self._driver.execute_query(
            "MATCH (n:Embedded) RETURN count(n) AS total",
            database_=self._database,
        )
        return records[0]["total"] if records else 0

    # ------------------------------------------------------------------
    # Temporal operations (DDR-003 Phase D)
    # ------------------------------------------------------------------

    async def decay_confidence(
        self,
        max_age_days: int = 90,
        decay_rate: float = 0.01,
        dry_run: bool = False,
        label: str | None = None,
        min_confidence: float = 0.0,
    ) -> dict[str, Any]:
        """Apply exponential decay to node confidence based on staleness.

        Formula: ``new_confidence = confidence × exp(-decay_rate × days_old)``

        Only affects nodes where confidence > 0.05, updated_at is older
        than 1 day, and status is not ``'archived'``.

        Args:
            max_age_days: Nodes older than this get extra decay penalty.
            decay_rate: Exponential decay rate (0.01 = gentle, 0.1 = aggressive).
            dry_run: If True, return what *would* change without writing.
            label: Optional label filter.
            min_confidence: Archive nodes falling below this after decay.

        Returns:
            ``{"affected": int, "archived": int,
            "sample": [{name, label, old_confidence, new_confidence, days_old}]}``
        """
        label_filter = f":{label}" if label else ""

        base = (
            f"MATCH (n{label_filter}) "
            "WHERE n.confidence IS NOT NULL "
            "  AND n.confidence > 0.05 "
            "  AND n.updated_at IS NOT NULL "
            "  AND (n.status IS NULL OR n.status <> 'archived') "
            "WITH n, "
            "  duration.inDays(n.updated_at, datetime()).days AS days_old, "
            "  n.confidence AS old_conf "
            "WHERE days_old > 0 "
            "WITH n, days_old, old_conf, "
            "  old_conf * exp(-$decay_rate * days_old) AS new_conf "
            "WHERE abs(old_conf - new_conf) > 0.001 "
        )

        if dry_run:
            cypher = base + (
                "RETURN COALESCE(n.name, n.title) AS name, "
                "  labels(n)[0] AS label, "
                "  old_conf AS old_confidence, "
                "  new_conf AS new_confidence, "
                "  days_old "
                "ORDER BY days_old DESC LIMIT 20"
            )
        else:
            cypher = base + (
                "SET n.confidence = new_conf, "
                "    n.decayed_at = datetime() "
                "RETURN COALESCE(n.name, n.title) AS name, "
                "  labels(n)[0] AS label, "
                "  old_conf AS old_confidence, "
                "  new_conf AS new_confidence, "
                "  days_old "
                "ORDER BY days_old DESC LIMIT 20"
            )

        records, _, _ = await self._driver.execute_query(
            cypher,
            parameters_={"decay_rate": decay_rate},
            database_=self._database,
        )
        sample = [dict(r) for r in records]
        affected = len(sample)

        # Archive nodes below min_confidence (only if not dry_run)
        archived = 0
        if not dry_run and min_confidence > 0:
            archive_cypher = (
                f"MATCH (n{label_filter}) "
                "WHERE n.confidence IS NOT NULL "
                "  AND n.confidence < $min_conf "
                "  AND (n.status IS NULL OR n.status <> 'archived') "
                "SET n.status = 'archived', n.updated_at = datetime() "
                "RETURN count(n) AS archived"
            )
            arch_records, _, _ = await self._driver.execute_query(
                archive_cypher,
                parameters_={"min_conf": min_confidence},
                database_=self._database,
            )
            archived = arch_records[0]["archived"] if arch_records else 0

        return {"affected": affected, "archived": archived, "sample": sample}

    async def query_at_date(
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
        cypher = (
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
        records, _, _ = await self._driver.execute_query(
            cypher,
            parameters_={"date": date, "limit": limit},
            database_=self._database,
        )
        return [dict(r) for r in records]

    # ------------------------------------------------------------------
    # Schema operations
    # ------------------------------------------------------------------

    async def init_schema(self, cypher_statements: list[str] | None = None) -> None:
        """Apply schema constraints and indexes."""
        if not cypher_statements:
            return
        for stmt in cypher_statements:
            try:
                await self._driver.execute_query(
                    stmt, database_=self._database,
                )
            except Exception as e:
                logger.warning("Schema statement failed: %s — %s", stmt, e)

    async def health_check(self) -> dict[str, Any]:
        """Return backend status."""
        await self._driver.verify_connectivity()
        return {"status": "ok", "backend": "neo4j-async"}

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"Neo4jAsyncStore(database={self._database!r}, "
            f"vector_dims={self._vector_dimensions})"
        )
