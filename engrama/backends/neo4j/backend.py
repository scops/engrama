"""
Engrama — Neo4j graph store.

Implements the ``GraphStore`` protocol using Neo4j's sync driver.
Public methods always return plain ``list[dict[str, Any]]`` — Node and
Relationship instances are converted at the boundary so callers never
import the ``neo4j`` package.
"""

from __future__ import annotations

from typing import Any

from neo4j import Record
from neo4j.graph import Node, Relationship
from neo4j.time import Date, DateTime, Duration, Time

from engrama.core.client import EngramaClient
from engrama.core.schema import TITLE_KEYED_LABELS

_NEO4J_TIME_TYPES = (DateTime, Date, Time, Duration)


def _to_python(value: Any) -> Any:
    """Recursively convert Neo4j driver types to plain Python.

    Node → ``{"_id", "_labels", **props}``; Relationship → ``{"_id",
    "_type", **props}``; temporal types → ISO-format strings; lists
    recurse; everything else passes through. The ``_*`` prefix lets
    callers tell metadata from real properties; ISO strings keep
    ordering and comparison consistent across backends (SQLite stores
    timestamps as ISO strings too).
    """
    if isinstance(value, Node):
        return {
            "_id": value.element_id,
            "_labels": list(value.labels),
            **{k: _to_python(v) for k, v in value.items()},
        }
    if isinstance(value, Relationship):
        return {
            "_id": value.element_id,
            "_type": value.type,
            **{k: _to_python(v) for k, v in value.items()},
        }
    if isinstance(value, _NEO4J_TIME_TYPES):
        return value.iso_format()
    if isinstance(value, list):
        return [_to_python(v) for v in value]
    return value


def _records_to_dicts(records: list[Record]) -> list[dict[str, Any]]:
    """Convert a list of Neo4j Records to plain dicts (no driver types leak)."""
    return [{k: _to_python(v) for k, v in r.items()} for r in records]


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

        Reserved for backend-internal use (vector store reuses the same
        connection). Skills and adapters should never reach for this —
        they speak the protocol via the named methods on this class.
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
    ) -> list[dict[str, Any]]:
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
            A list with one dict shaped ``{"n": {"_id", "_labels", **props}}``.
        """
        # Extract temporal fields from properties (if supplied)
        valid_from = properties.pop("valid_from", None)
        confidence = properties.pop("confidence", None)

        set_clauses_create: list[str] = [
            "n.created_at = datetime()",
            "n.updated_at = datetime()",
            "n.valid_from = $valid_from",
            "n.confidence = $confidence_val",
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

        return _records_to_dicts(self._client.run(query, params))

    def get_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
    ) -> dict[str, Any] | None:
        """Retrieve a single node by its unique key."""
        query = f"MATCH (n:{label} {{{key_field}: $key_value}}) RETURN n"
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

        When ``soft=True``, sets ``status='archived'``, ``archived_at``
        and ``updated_at``.  When ``soft=False``, detach-deletes the node.
        """
        if soft:
            query = (
                f"MATCH (n:{label} {{{key_field}: $key_value}}) "
                "SET n.status = 'archived', n.archived_at = datetime(), "
                "    n.updated_at = datetime() "
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
        return _records_to_dicts(self._client.run(query, {"date": date, "limit": limit}))

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
    ) -> list[dict[str, Any]]:
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
        return _records_to_dicts(self._client.run(query, params))

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
    ) -> list[dict[str, Any]]:
        """Traverse N hops from a node and return its neighbourhood.

        Returns a list of records, each shaped ``{"start": <node-dict>,
        "rel": [<rel-dict>, ...], "neighbour": <node-dict>}``.  Node and
        relationship dicts carry ``_id``, ``_labels`` / ``_type`` plus
        their properties.
        """
        query = (
            f"MATCH (start:{label} {{{key_field}: $key_value}})"
            f"-[rel*1..{hops}]-(neighbour) "
            "RETURN start, rel, neighbour"
        )
        return _records_to_dicts(self._client.run(query, {"key_value": key_value}))

    def fulltext_search(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Keyword search against the ``memory_search`` fulltext index.

        Returns records with ``type``, ``name``, ``score``, enrichment
        fields (``summary``, ``tags``) and temporal fields (``confidence``,
        ``updated_at``) for Phase D scoring.

        ``summary`` falls back to ``description`` when absent so nodes
        stored before the enrichment fields existed keep returning useful
        context.  ``details`` is intentionally excluded from search results
        to keep responses compact — callers can use ``engrama_context`` for
        the full content.
        """
        cypher = (
            'CALL db.index.fulltext.queryNodes("memory_search", $query) '
            "YIELD node, score "
            "RETURN labels(node)[0] AS type, "
            "COALESCE(node.name, node.title) AS name, "
            "score, "
            "COALESCE(node.summary, node.description, '') AS summary, "
            "node.tags AS tags, "
            "node.confidence AS confidence, "
            "toString(node.updated_at) AS updated_at "
            "ORDER BY score DESC LIMIT $limit"
        )
        return _records_to_dicts(self._client.run(cypher, {"query": query, "limit": limit}))

    def run_cypher(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a raw Cypher query.

        Delegates to :meth:`EngramaClient.run` and converts driver types
        to plain dicts so callers don't import ``neo4j``.
        """
        return _records_to_dicts(self._client.run(query, params))

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

    # ------------------------------------------------------------------
    # Forget operations (skills/forget.py)
    # ------------------------------------------------------------------

    def archive_node_by_name(
        self,
        label: str,
        name: str,
        *,
        purge: bool = False,
    ) -> dict[str, Any]:
        """Archive (or DETACH DELETE) a node by ``(label, name|title)``.

        The merge-key (``name`` vs ``title``) is selected from
        :data:`TITLE_KEYED_LABELS`.

        Returns a dict with:

        * ``matched`` (bool) — at least one node existed.
        * ``deleted`` (int) — DETACH DELETE count when ``purge=True``,
          ``0`` when archiving.
        """
        merge_key = "title" if label in TITLE_KEYED_LABELS else "name"

        if purge:
            query = (
                f"MATCH (n:{label} {{{merge_key}: $name}}) "
                "DETACH DELETE n "
                "RETURN count(*) AS deleted"
            )
            records = self._client.run(query, {"name": name})
            deleted = records[0]["deleted"] if records else 0
            return {"matched": deleted > 0, "deleted": deleted}

        query = (
            f"MATCH (n:{label} {{{merge_key}: $name}}) "
            "SET n.status = 'archived', n.archived_at = datetime(), "
            "    n.updated_at = datetime() "
            "RETURN n"
        )
        records = self._client.run(query, {"name": name})
        return {"matched": len(records) > 0, "deleted": 0}

    def archive_nodes_older_than(
        self,
        label: str,
        days: int,
        *,
        purge: bool = False,
    ) -> dict[str, Any]:
        """Archive (or DETACH DELETE) nodes whose ``updated_at`` is older
        than *days* days.

        Returns ``{"affected": int}``.
        """
        if purge:
            query = (
                f"MATCH (n:{label}) "
                "WHERE n.updated_at IS NOT NULL "
                "  AND n.updated_at < datetime() - duration({days: $days}) "
                "DETACH DELETE n "
                "RETURN count(*) AS affected"
            )
        else:
            query = (
                f"MATCH (n:{label}) "
                "WHERE n.updated_at IS NOT NULL "
                "  AND n.updated_at < datetime() - duration({days: $days}) "
                "  AND (n.status IS NULL OR n.status <> 'archived') "
                "SET n.status = 'archived', n.archived_at = datetime(), "
                "    n.updated_at = datetime() "
                "RETURN count(n) AS affected"
            )

        records = self._client.run(query, {"days": days})
        affected = records[0]["affected"] if records else 0
        return {"affected": affected}

    # ------------------------------------------------------------------
    # Insight operations (skills/proactive.py + skills/reflect.py)
    # ------------------------------------------------------------------

    def get_pending_insights(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return pending Insights ordered by confidence (highest first),
        breaking ties by ``created_at`` (newest first).
        """
        records = self._client.run(
            "MATCH (i:Insight {status: $status}) "
            "RETURN i.title AS title, i.body AS body, "
            "       i.confidence AS confidence, "
            "       i.source_query AS source_query, "
            "       i.created_at AS created_at "
            "ORDER BY i.confidence DESC, i.created_at DESC "
            "LIMIT $limit",
            {"status": "pending", "limit": limit},
        )
        return [dict(r) for r in records]

    def update_insight_status(self, title: str, new_status: str) -> bool:
        """Set ``status`` and the matching timestamp (``approved_at`` /
        ``dismissed_at``) on an Insight node.
        """
        ts_field = "approved_at" if new_status == "approved" else "dismissed_at"
        query = (
            "MATCH (i:Insight {title: $title}) "
            f"SET i.status = $new_status, "
            f"    i.{ts_field} = datetime(), "
            "    i.updated_at = datetime() "
            "RETURN i.title AS title"
        )
        records = self._client.run(
            query,
            {"title": title, "new_status": new_status},
        )
        return len(records) > 0

    def get_insight_by_title(self, title: str) -> dict[str, Any] | None:
        """Fetch an Insight by exact title.

        Returns ``{status, body, confidence, source_query}`` or ``None``.
        """
        records = self._client.run(
            "MATCH (i:Insight {title: $title}) "
            "RETURN i.status AS status, i.body AS body, "
            "       i.confidence AS confidence, "
            "       i.source_query AS source_query",
            {"title": title},
        )
        if records:
            return dict(records[0])
        return None

    def mark_insight_synced(self, title: str, obsidian_path: str) -> bool:
        """Set ``obsidian_path`` + ``synced_at`` + ``updated_at`` on an
        Insight node.  Returns ``True`` if the Insight existed.
        """
        records = self._client.run(
            "MATCH (i:Insight {title: $title}) "
            "SET i.obsidian_path = $path, "
            "    i.synced_at = datetime(), "
            "    i.updated_at = datetime() "
            "RETURN i.title AS title",
            {"title": title, "path": obsidian_path},
        )
        return len(records) > 0

    def get_dismissed_insight_titles(self) -> set[str]:
        """Return titles of all dismissed Insights."""
        records = self._client.run(
            "MATCH (i:Insight {status: 'dismissed'}) RETURN i.title AS title",
            {},
        )
        return {r["title"] for r in records}

    def find_insight_by_source_query(
        self,
        source_query: str,
        statuses: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Find an Insight by ``source_query`` and optional status filter.

        Async-equivalent: :meth:`Neo4jAsyncStore.find_insight_by_source_query`.
        Default status set is ``["pending", "approved"]``.
        """
        status_list = statuses or ["pending", "approved"]
        records = self._client.run(
            "MATCH (i:Insight {source_query: $sq}) "
            "WHERE i.status IN $statuses "
            "RETURN i.title AS title, i.status AS status LIMIT 1",
            {"sq": source_query, "statuses": status_list},
        )
        if records:
            return dict(records[0])
        return None

    # ------------------------------------------------------------------
    # Reflect — pattern detection (skills/reflect.py)
    # ------------------------------------------------------------------

    def detect_cross_project_solutions(self) -> list[dict[str, Any]]:
        """Open Problem shares a Concept with a resolved Problem in a
        different Project that has a Decision.
        """
        cypher = (
            "MATCH (pB:Project)-[:HAS]->(open:Problem {status: $open_status}) "
            "MATCH (open)-[:INSTANCE_OF|APPLIES]->(c:Concept)"
            "<-[:INSTANCE_OF|APPLIES]-(resolved:Problem {status: $resolved_status}) "
            "MATCH (resolved)-[:SOLVED_BY]->(d:Decision)<-[:INFORMED_BY]-(pA:Project) "
            "WHERE pA <> pB "
            "RETURN pB.name AS target_project, open.title AS open_problem, "
            "d.title AS decision, pA.name AS source_project, c.name AS concept"
        )
        records = self._client.run(
            cypher,
            {"open_status": "open", "resolved_status": "resolved"},
        )
        return [dict(r) for r in records]

    def detect_shared_technology(self) -> list[dict[str, Any]]:
        """Two distinct entities use the same Technology."""
        cypher = (
            "MATCH (a)-[:USES|TEACHES|COMPOSED_OF]->(t:Technology)"
            "<-[:USES|TEACHES|COMPOSED_OF]-(b) "
            "WHERE id(a) < id(b) "
            "AND NOT a:Insight AND NOT b:Insight "
            "RETURN coalesce(a.name, a.title) AS entity_a, labels(a)[0] AS type_a, "
            "coalesce(b.name, b.title) AS entity_b, labels(b)[0] AS type_b, "
            "t.name AS technology"
        )
        records = self._client.run(cypher, {})
        return [dict(r) for r in records]

    def detect_training_opportunities(self) -> list[dict[str, Any]]:
        """A Vulnerability or open Problem shares a Concept with a Course."""
        cypher = (
            "MATCH (issue)-[:INSTANCE_OF|APPLIES]->(c:Concept)<-[:COVERS]-(course:Course) "
            "WHERE (issue:Vulnerability) OR (issue:Problem AND issue.status = $open_status) "
            "RETURN coalesce(issue.title, issue.name) AS issue, "
            "labels(issue)[0] AS issue_type, c.name AS concept, course.name AS course"
        )
        records = self._client.run(cypher, {"open_status": "open"})
        return [dict(r) for r in records]

    def detect_technique_transfer(self) -> list[dict[str, Any]]:
        """Technique used in domain A could apply in domain B."""
        cypher = (
            "MATCH (t:Technique)-[:IN_DOMAIN]->(d1:Domain) "
            "MATCH (d2:Domain) WHERE d1 <> d2 "
            "AND NOT EXISTS { MATCH (t)-[:IN_DOMAIN]->(d2) } "
            "MATCH (other)-[:IN_DOMAIN]->(d2) "
            "WHERE (other)-[:INSTANCE_OF|APPLIES]->(:Concept)<-[:INSTANCE_OF|APPLIES]-(t) "
            "RETURN t.name AS technique, d1.name AS source_domain, "
            "d2.name AS target_domain, count(other) AS related_entities "
            "ORDER BY related_entities DESC LIMIT 10"
        )
        records = self._client.run(cypher, {})
        return [dict(r) for r in records]

    def detect_concept_clusters(self) -> list[dict[str, Any]]:
        """Concept connected to >= 3 entities."""
        cypher = (
            "MATCH (c:Concept)<-[:INSTANCE_OF|APPLIES]-(n) "
            "WITH c, collect(DISTINCT {name: coalesce(n.name, n.title), "
            "label: labels(n)[0]}) AS connected, count(n) AS cnt "
            "WHERE cnt >= 3 "
            "RETURN c.name AS concept, cnt AS entity_count, connected[..5] AS sample "
            "ORDER BY cnt DESC LIMIT 10"
        )
        records = self._client.run(cypher, {})
        return [dict(r) for r in records]

    def detect_stale_knowledge(self) -> list[dict[str, Any]]:
        """Nodes 90d+ stale or low-confidence connected to active
        Project/Course.
        """
        cypher = (
            "MATCH (n)-[r]-(active) "
            "WHERE (active:Project OR active:Course) "
            "AND (active.status IS NULL OR active.status IN [$active_status, 'active']) "
            "AND ("
            "  n.updated_at < datetime() - duration({days: 90}) "
            "  OR (n.confidence IS NOT NULL AND n.confidence < 0.3)"
            ") "
            "AND NOT n:Project AND NOT n:Course AND NOT n:Domain "
            "RETURN coalesce(n.name, n.title) AS name, labels(n)[0] AS label, "
            "n.updated_at AS last_updated, n.confidence AS confidence, "
            "active.name AS project, type(r) AS rel "
            "ORDER BY coalesce(n.confidence, 1.0) ASC, n.updated_at ASC LIMIT 15"
        )
        records = self._client.run(cypher, {"active_status": "active"})
        return [dict(r) for r in records]

    def detect_under_connected_nodes(self) -> list[dict[str, Any]]:
        """Nodes with fewer than 2 *substantive* relationships.

        Edges to neighbours with ``status = 'stub'`` are not counted —
        stubs are placeholder nodes and treating them as real
        connections hides genuinely under-connected nodes.
        """
        cypher = (
            "MATCH (n) WHERE NOT n:Domain AND NOT n:Insight "
            "AND (n.name IS NOT NULL OR n.title IS NOT NULL) "
            "AND n.status <> 'archived' "
            "WITH n, size([(n)-[]-(m) "
            "WHERE coalesce(m.status, 'active') <> 'stub' | 1]) AS rel_count "
            "WHERE rel_count < 2 "
            "RETURN coalesce(n.name, n.title) AS name, labels(n)[0] AS label, "
            "rel_count, n.created_at AS created "
            "ORDER BY n.created_at DESC LIMIT 15"
        )
        records = self._client.run(cypher, {})
        return [dict(r) for r in records]

    # ------------------------------------------------------------------
    # Associate (skills/associate.py)
    # ------------------------------------------------------------------

    def find_obsidian_path(self, label: str, name: str) -> str | None:
        """Return ``n.obsidian_path`` for the node identified by
        ``(label, name|title)``, or ``None`` if absent.
        """
        merge_key = "title" if label in TITLE_KEYED_LABELS else "name"
        records = self._client.run(
            f"MATCH (n:{label} {{{merge_key}: $name}}) RETURN n.obsidian_path AS path",
            {"name": name},
        )
        if records and records[0]["path"]:
            return records[0]["path"]
        return None

    # ------------------------------------------------------------------
    # Obsidian sync (adapters/obsidian/sync.py)
    # ------------------------------------------------------------------

    def list_documented_nodes(self) -> list[dict[str, Any]]:
        """Return nodes that have an ``obsidian_path`` — used by
        ``ObsidianSync.archive_missing``.

        Each entry is ``{label, name, path}``.
        """
        records = self._client.run(
            "MATCH (n) WHERE n.obsidian_path IS NOT NULL "
            "RETURN labels(n)[0] AS label, n.name AS name, n.obsidian_path AS path",
            {},
        )
        return [dict(r) for r in records]

    def archive_node_for_missing_note(self, label: str, name: str) -> bool:
        """Archive a node whose Obsidian note no longer exists.

        Differs from :meth:`archive_node_by_name`: matches via
        ``$label IN labels(n)`` rather than ``(n:Label {name})``.  Sets
        the same archive shape (``status`` + ``archived_at`` +
        ``updated_at``) as the other soft-archive methods.  Returns
        ``True`` if a node was matched.
        """
        records = self._client.run(
            "MATCH (n {name: $name}) WHERE $label IN labels(n) "
            "SET n.status = 'archived', n.archived_at = datetime(), "
            "    n.updated_at = datetime() "
            "RETURN n.name AS name",
            {"name": name, "label": label},
        )
        return len(records) > 0

    def merge_wiki_link(
        self,
        *,
        from_label: str,
        from_name: str,
        to_label: str,
        to_name: str,
    ) -> None:
        """``MERGE (a)-[:LINKS_TO]->(b)`` where both endpoints are matched
        via ``$label IN labels(n)`` on the ``name`` property.
        """
        self._client.run(
            "MATCH (a {name: $from_name}) "
            "WHERE $from_label IN labels(a) "
            "MATCH (b {name: $to_name}) "
            "WHERE $to_label IN labels(b) "
            "MERGE (a)-[:LINKS_TO]->(b)",
            {
                "from_name": from_name,
                "from_label": from_label,
                "to_name": to_name,
                "to_label": to_label,
            },
        )

    def merge_wiki_link_by_target_name(
        self,
        *,
        from_label: str,
        from_name: str,
        target_name: str,
    ) -> int:
        """Resolve the target node by ``toLower(name)`` and ``MERGE
        (a)-[:LINKS_TO]->(b)``.

        Returns ``1`` if the query executed (mirrors the previous
        unconditional counter increment in ``ObsidianSync._resolve_single_note_links``).
        """
        self._client.run(
            "MATCH (b) WHERE toLower(b.name) = toLower($target) "
            "WITH b LIMIT 1 "
            "MATCH (a {name: $from_name}) WHERE $from_label IN labels(a) "
            "MERGE (a)-[:LINKS_TO]->(b)",
            {
                "target": target_name,
                "from_name": from_name,
                "from_label": from_label,
            },
        )
        return 1

    def lookup_node_label(self, name: str) -> str | None:
        """Return the primary label of the node whose ``name`` (or
        ``title``, for nodes that use ``title`` instead of ``name`` such
        as ``Decision`` / ``Problem``) matches case-insensitively.
        """
        records = self._client.run(
            "MATCH (n) WHERE toLower(COALESCE(n.name, n.title)) = toLower($name) "
            "RETURN labels(n)[0] AS label LIMIT 1",
            {"name": name},
        )
        if records:
            return records[0]["label"]
        return None

    # ------------------------------------------------------------------
    # CLI helpers (engrama/cli.py)
    # ------------------------------------------------------------------

    def apply_schema_statements(
        self,
        statements: list[str],
    ) -> list[tuple[str, Exception]]:
        """Execute schema statements one at a time.

        Returns the list of ``(statement, exception)`` pairs for failed
        statements (in order).  The CLI decides whether to print warnings
        or ignore (e.g. unsupported ``SHOW`` statements on certain Neo4j
        editions).
        """
        failures: list[tuple[str, Exception]] = []
        for stmt in statements:
            try:
                self._client.run(stmt)
            except Exception as e:
                failures.append((stmt, e))
        return failures

    def seed_domain(self, name: str, description: str) -> None:
        """``MERGE (d:Domain {name})`` with description + timestamps.

        Delegates to :meth:`merge_node` so seed nodes get the same
        DDR-003 temporal fields (``valid_from``, ``confidence``) as the
        rest of the graph.  ``description`` is refreshed on MATCH (the
        canonical seed in :data:`_MODULE_SEEDS` wins on every run).
        """
        self.merge_node("Domain", "name", name, {"description": description})

    def seed_concept_in_domain(
        self,
        concept_name: str,
        domain_name: str,
    ) -> None:
        """``MERGE`` a Concept and link it ``IN_DOMAIN`` to a Domain.

        Implemented via :meth:`merge_node` + :meth:`merge_relation`.  If
        the Domain does not exist (``seed_domain`` failed earlier) the
        relation is silently skipped, matching the previous semantics.
        """
        self.merge_node("Concept", "name", concept_name, {})
        self.merge_relation(
            "Concept",
            "name",
            concept_name,
            "IN_DOMAIN",
            "Domain",
            "name",
            domain_name,
        )

    def list_nodes_for_embedding(
        self,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """List nodes for re-embedding.

        With ``force=False`` skips nodes already labelled ``:Embedded``;
        with ``force=True`` returns every node.

        Returns ``[{eid, labels, props}, ...]``.
        """
        records = self._client.run(
            "MATCH (n) WHERE NOT 'Embedded' IN labels(n) OR $force "
            "RETURN elementId(n) AS eid, labels(n) AS labels, "
            "properties(n) AS props",
            {"force": force},
        )
        return [dict(r) for r in records]
