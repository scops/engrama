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

from engrama.backends.neo4j import _reflect_cypher
from engrama.backends.neo4j._cypher import escape_cypher_identifier, scoped_key_lookup
from engrama.backends.neo4j._lucene import escape_lucene_query
from engrama.core.client import EngramaClient
from engrama.core.health import tag_anchor_rows
from engrama.core.resolve import name_fragments
from engrama.core.schema import TITLE_KEYED_LABELS
from engrama.core.scope import (
    MemoryScope,
    node_owner,
    owner_filter_cypher,
    scope_filter_cypher,
)
from engrama.core.stubs import clears_stub

_NEO4J_TIME_TYPES = (DateTime, Date, Time, Duration)

# Server-managed timestamps: always set by the store itself via Cypher
# ``datetime()``, never taken from the caller's property bag. The engine
# and MCP handlers already strip these, but the cross-backend importer
# (``engrama import``) replays raw export dicts straight through
# ``merge_node`` — and exports carry ``updated_at`` as an ISO string. If
# that string is written verbatim it clobbers the ``datetime()`` and the
# property becomes STRING-typed, breaking ``duration.between(...)`` in
# decay / ``query_at_date``. Enforce the invariant here. See #76.
_SERVER_MANAGED_TIMESTAMPS = frozenset({"created_at", "updated_at"})

# Domain temporal properties a caller MAY set explicitly. When supplied
# (notably by the importer, where they arrive as ISO strings) they must
# be coerced to a Neo4j datetime via ``datetime($p)``, never stored raw.
_TEMPORAL_PROPERTIES = frozenset(
    {
        "valid_from",
        "valid_to",
        "archived_at",
        "synced_at",
        "approved_at",
        "dismissed_at",
        "decayed_at",
    }
)


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

        The node is keyed on ``(label, key, owner)`` where the owner is the
        ``org_id``/``user_id`` in ``properties`` (see
        :func:`~engrama.core.scope.node_owner`), so two owners writing the
        same name get two separate nodes.

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
        owner = node_owner(properties)

        # Extract temporal fields from properties (if supplied)
        valid_from = properties.pop("valid_from", None)
        confidence = properties.pop("confidence", None)

        # Server-managed timestamps are never taken from the caller (see
        # _SERVER_MANAGED_TIMESTAMPS): drop them so a caller-supplied ISO
        # string can't override the datetime() set below.
        for _ts in _SERVER_MANAGED_TIMESTAMPS:
            properties.pop(_ts, None)

        set_clauses_create: list[str] = [
            "n.created_at = datetime()",
            "n.updated_at = datetime()",
            "n.valid_from = $valid_from",
            "n.confidence = $confidence_val",
        ]
        set_clauses_match: list[str] = [
            "n.updated_at = datetime()",
        ]
        if clears_stub(properties):
            # Enriching a stub promotes it (DDR-006).
            set_clauses_match.append(
                "n.status = CASE WHEN n.status = 'stub' THEN 'active' ELSE n.status END"
            )

        params: dict[str, Any] = {
            "merge_value": key_value,
            "valid_from": valid_from or "$$NOW$$",  # sentinel replaced below
            "confidence_val": confidence if confidence is not None else 1.0,
        }

        # An explicit confidence is a statement about the fact, so it applies
        # on update too (DDR-007); absent, an existing value is left alone.
        if confidence is not None:
            set_clauses_match.append("n.confidence = $confidence_val")

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
            # Coerce temporal properties to a Neo4j datetime so an
            # ISO-string value (e.g. from the importer) is never stored
            # as a raw string. See #76.
            rhs = f"datetime(${param_name})" if key in _TEMPORAL_PROPERTIES else f"${param_name}"
            # Property keys are caller-supplied and not whitelisted upstream, so
            # backtick-quote them: a malformed key can never break out of the
            # SET clause and corrupt the query. See ``_cypher``.
            col = escape_cypher_identifier(key)
            set_clauses_create.append(f"n.{col} = {rhs}")
            set_clauses_match.append(f"n.{col} = {rhs}")
            params[param_name] = value

        if embedding is not None:
            set_clauses_create.append("n.embedding = $embedding")
            set_clauses_match.append("n.embedding = $embedding")
            params["embedding"] = embedding

        on_create = ", ".join(set_clauses_create)
        on_match = ", ".join(set_clauses_match)

        owner_clause, owner_params = owner_filter_cypher(owner, "n")
        if owner is not None and owner.org_id and owner.user_id:
            # The owner is part of the MERGE pattern, so the scoped
            # uniqueness constraint (name, org_id, user_id) backs it.
            params.update(owner_params)
            query = (
                f"MERGE (n:{label} {{{key_field}: $merge_value, "
                "org_id: $owner_org_id, user_id: $owner_user_id}) "
                f"ON CREATE SET {on_create} "
                f"ON MATCH SET {on_match} "
                "RETURN n"
            )
        else:
            # Identity-less or partial (legacy/admin) owner: MERGE can't
            # express "property absent", so resolve the exact node first.
            existing = self._client.run(
                f"MATCH (n:{label} {{{key_field}: $merge_value}}) WHERE {owner_clause} "
                "RETURN elementId(n) AS eid LIMIT 1",
                {"merge_value": key_value, **owner_params},
            )
            if existing:
                params["eid"] = existing[0]["eid"]
                query = f"MATCH (n) WHERE elementId(n) = $eid SET {on_match} RETURN n"
            else:
                query = f"CREATE (n:{label} {{{key_field}: $merge_value}}) SET {on_create} RETURN n"

        return _records_to_dicts(self._client.run(query, params))

    def node_degree(
        self,
        label: str,
        key_field: str,
        key_value: str,
        scope: MemoryScope | None = None,
    ) -> int | None:
        """Substantive degree of the node visible in ``scope`` (own first);
        edges to reflect-generated Insights don't count. ``None`` if unseen."""
        cypher, params = scoped_key_lookup(label, key_field, scope)
        cypher += (
            " RETURN size([(n)-[]-(m) "
            "WHERE NOT (m:Insight AND m.source_query IS NOT NULL) | 1]) AS degree"
        )
        records = self._client.run(cypher, {"key_value": key_value, **params})
        return records[0]["degree"] if records else None

    def get_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        scope: MemoryScope | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a single node by ``(label, key)``.

        Names are only unique per owner, so a tenant read passes ``scope``
        (visible nodes only, own first); ``None`` is the unscoped admin lookup.
        """
        query, params = scoped_key_lookup(label, key_field, scope)
        records = self._client.run(query + " RETURN n", {"key_value": key_value, **params})
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
        and ``archived_reason`` (``updated_at`` is left alone: archiving is
        not activity). When ``soft=False``, detach-deletes the node.
        """
        if soft:
            query = (
                f"MATCH (n:{label} {{{key_field}: $key_value}}) "
                "SET n.status = 'archived', n.archived_at = datetime(), "
                "    n.archived_reason = 'delete' "
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

        Deprecated (DDR-007): no longer called by Engrama; removed in a future release.

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
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Create a relationship between two existing nodes (idempotent).

        If either endpoint does not exist, the relationship simply won't
        be created (no error).

        Endpoints are matched the SAME permissive way ``lookup_node_label``
        resolves them — ``toLower(COALESCE(name, title))`` — so lookup and
        merge can't disagree on a node's key (#93, mode 2).

        Spec 001 FR-1: when ``scope`` is supplied, BOTH endpoints are
        scope-filtered (not just the edge stamped), closing the scope
        asymmetry that let a tenant reach another tenant's nodes here.
        ``scope=None`` keeps the legacy unscoped admin / import path.
        """
        set_clause = ""
        params: dict[str, Any] = {"from_value": from_value, "to_value": to_value}
        where_a = "toLower(COALESCE(a.name, a.title)) = toLower($from_value)"
        where_b = "toLower(COALESCE(b.name, b.title)) = toLower($to_value)"
        if scope is not None and scope.org_id and scope.user_id:
            a_clause, scope_params = scope_filter_cypher(scope, "a")
            b_clause, _ = scope_filter_cypher(scope, "b")
            where_a = f"{where_a} AND {a_clause}"
            where_b = f"{where_b} AND {b_clause}"
            params.update(scope_params)
            set_clause = (
                "SET r.org_id = coalesce(r.org_id, $scope_org_id), "
                "    r.user_id = coalesce(r.user_id, $scope_user_id) "
            )
        query = (
            f"MATCH (a:{from_label}) WHERE {where_a} "
            f"MATCH (b:{to_label}) WHERE {where_b} "
            f"WITH a, b LIMIT 1 "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            f"{set_clause}"
            "RETURN type(r) AS rel_type"
        )
        return _records_to_dicts(self._client.run(query, params))

    # ------------------------------------------------------------------
    # Migration helpers (iter_all_*, purge_all) — used by engrama
    # export / import. Not on the GraphStore protocol because they only
    # make sense for the bulk-migration code path.
    # ------------------------------------------------------------------

    def iter_all_nodes(self):
        """Yield every non-Insight-stub node as ``{label, key_field,
        key_value, properties}``. ``key_field`` is ``'title'`` when the
        node carries a title and no name, ``'name'`` otherwise. Properties
        exclude the synthetic ``embedding`` array — vectors come back via
        :meth:`Neo4jVectorStore.iter_all_vectors`. Driver types
        (``DateTime``, ``Node``, ``Relationship``) are passed through
        :func:`_to_python` so the dump is plain JSON.
        """
        records = self._client.run(
            "MATCH (n) "
            "WITH n, [l IN labels(n) WHERE l <> 'Embedded'][0] AS label "
            "WHERE label IS NOT NULL "
            "RETURN label, "
            "       n.name  AS name, "
            "       n.title AS title, "
            "       properties(n) AS props"
        )
        for r in records:
            name = r.get("name")
            title = r.get("title")
            raw_props = r.get("props") or {}
            props = {k: _to_python(v) for k, v in raw_props.items()}
            props.pop("embedding", None)  # vectors are dumped separately
            if name:
                key_field, key_value = "name", name
            elif title:
                key_field, key_value = "title", title
            else:
                # Node without a merge key — orphan from manual cypher.
                # Skip rather than emit a record we can't re-import.
                continue
            yield {
                "label": r["label"],
                "key_field": key_field,
                "key_value": key_value,
                "properties": props,
            }

    def restore_timestamps(
        self,
        label: str,
        key_value: str,
        owner: MemoryScope | None,
        created_at: str | None,
        updated_at: str | None,
    ) -> bool:
        """Importer-only: put back the original ``created_at`` / ``updated_at``.

        ``merge_node`` never takes these from a caller (#76), so an import
        would otherwise date every node to the day it ran (DDR-007). This is
        the trusted path for that one caller; ``None`` values are left alone.
        """
        # scope-exempt: import/migration path — addresses the exact owner's
        # node that the importer just wrote.
        key_field = "title" if label in TITLE_KEYED_LABELS else "name"
        owner_clause, owner_params = owner_filter_cypher(owner, "n")
        records = self._client.run(
            f"MATCH (n:{label} {{{key_field}: $key_value}}) WHERE {owner_clause} "
            "SET n.created_at = coalesce(datetime($created_at), n.created_at), "
            "    n.updated_at = coalesce(datetime($updated_at), n.updated_at) "
            "RETURN count(n) AS n",
            {
                "key_value": key_value,
                "created_at": created_at,
                "updated_at": updated_at,
                **owner_params,
            },
        )
        return bool(records and records[0]["n"])

    def health_snapshot(self, scope: MemoryScope | None = None) -> dict[str, Any]:
        """Scoped nodes and edges for :func:`engrama.core.health.compute_health`.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → empty snapshot.
        Only edges whose two endpoints are visible in ``scope`` are returned.
        """
        nodes = [dict(r) for r in self._client.run(*_reflect_cypher.health_nodes(scope))]
        edges = [(r["a"], r["b"]) for r in self._client.run(*_reflect_cypher.health_edges(scope))]
        return {"nodes": nodes, "edges": edges}

    def name_candidates(
        self, name: str, scope: MemoryScope | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """In-scope nodes sharing a name fragment with ``name`` (DDR-006)."""
        frags = name_fragments(name)
        if not frags:
            return []
        return [
            dict(r)
            for r in self._client.run(*_reflect_cypher.name_candidates(name, frags, scope, limit))
        ]

    def list_anchors(self, scope: MemoryScope | None = None) -> list[dict[str, str]]:
        """Live anchor nodes in ``scope`` as ``{label, name}``."""
        return [dict(r) for r in self._client.run(*_reflect_cypher.anchors(scope))]

    def detect_tags_without_edge(self, scope: MemoryScope | None = None) -> list[dict[str, Any]]:
        """Reflect detector: anchors named by tags on nodes not linked to them."""
        return tag_anchor_rows(self.health_snapshot(scope))

    def iter_all_relations(self):
        """Yield every relationship as ``{from_label, from_key, from_value,
        rel_type, to_label, to_key, to_value}``. Mirrors the SQLite
        backend's edge dump, including the edge's ``org_id``/``user_id``.
        """
        records = self._client.run(
            "MATCH (a)-[r]->(b) "
            "WITH r, "
            "     [l IN labels(a) WHERE l <> 'Embedded'][0] AS from_label, "
            "     [l IN labels(b) WHERE l <> 'Embedded'][0] AS to_label, "
            "     a, b "
            "WHERE from_label IS NOT NULL AND to_label IS NOT NULL "
            "RETURN from_label, "
            "       a.name  AS from_name, "
            "       a.title AS from_title, "
            "       type(r) AS rel_type, "
            "       to_label, "
            "       b.name  AS to_name, "
            "       b.title AS to_title, "
            "       r.org_id AS org_id, "
            "       r.user_id AS user_id"
        )
        for r in records:
            from_field, from_value = (
                ("name", r["from_name"]) if r["from_name"] else ("title", r["from_title"])
            )
            to_field, to_value = (
                ("name", r["to_name"]) if r["to_name"] else ("title", r["to_title"])
            )
            if not from_value or not to_value:
                continue
            yield {
                "from_label": r["from_label"],
                "from_key": from_field,
                "from_value": from_value,
                "rel_type": r["rel_type"],
                "to_label": r["to_label"],
                "to_key": to_field,
                "to_value": to_value,
                "org_id": r["org_id"],
                "user_id": r["user_id"],
            }

    def purge_all(self) -> None:
        """Wipe every node and relationship from the database. Vector
        index entries are tied to the nodes and disappear with them.
        """
        self._client.run("MATCH (n) DETACH DELETE n")

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
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Traverse N hops from a node and return its neighbourhood.

        Returns a list of records, each shaped ``{"start": <node-dict>,
        "rel": [<rel-dict>, ...], "neighbour": <node-dict>}``.  Node and
        relationship dicts carry ``_id``, ``_labels`` / ``_type`` plus
        their properties.

        DDR-003 Phase F: when ``scope`` is set, both the start node and
        each returned neighbour must match the scope-visibility rule.
        """
        start_clause, start_params = scope_filter_cypher(scope, "start")
        nb_clause, nb_params = scope_filter_cypher(scope, "neighbour")
        where_parts: list[str] = []
        if start_clause:
            where_parts.append(start_clause)
        if nb_clause:
            where_parts.append(nb_clause)
        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        query = (
            f"MATCH (start:{label} {{{key_field}: $key_value}})"
            f"-[rel*1..{hops}]-(neighbour) "
            f"{where_sql} "
            "RETURN start, rel, neighbour"
        )
        params: dict[str, Any] = {"key_value": key_value, **start_params, **nb_params}
        return _records_to_dicts(self._client.run(query, params))

    def fulltext_search(
        self,
        query: str,
        limit: int = 10,
        scope: MemoryScope | None = None,
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

        DDR-003 Phase F: when ``scope`` is set, results are filtered by
        the scope-visibility rule (see :mod:`engrama.core.scope`).
        """
        scope_clause, scope_params = scope_filter_cypher(scope, "node")
        # Archived nodes are the "trash" — excluded from search (a forgotten
        # node must not resurface). Mirrors the graph-search/decay filters.
        where_parts = ["(node.status IS NULL OR node.status <> 'archived')"]
        if scope_clause:
            where_parts.append(scope_clause)
        where_sql = "WHERE " + " AND ".join(where_parts) + " "
        cypher = (
            'CALL db.index.fulltext.queryNodes("memory_search", $query) '
            "YIELD node, score "
            f"{where_sql}"
            "RETURN labels(node)[0] AS type, "
            "COALESCE(node.name, node.title) AS name, "
            "score, "
            "COALESCE(node.summary, node.description, '') AS summary, "
            "node.tags AS tags, "
            "node.confidence AS confidence, "
            "node.trust_level AS trust_level, "
            "toString(node.updated_at) AS updated_at "
            "ORDER BY score DESC LIMIT $limit"
        )
        params: dict[str, Any] = {
            "query": escape_lucene_query(query),
            "limit": limit,
            **scope_params,
        }
        return _records_to_dicts(self._client.run(cypher, params))

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

    def count_labels(
        self,
        scope: MemoryScope | None = None,
    ) -> dict[str, int]:
        """Count nodes per label within ``scope``. Used by reflect to profile
        the caller's slice of the graph.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → empty dict.
        """
        scope_clause, scope_params = scope_filter_cypher(scope, "n")
        where_sql = f"AND {scope_clause} " if scope_clause else ""
        records = self._client.run(
            "MATCH (n) WHERE NOT n:Insight "
            f"{where_sql}"
            "RETURN labels(n)[0] AS label, count(n) AS cnt "
            "ORDER BY cnt DESC",
            scope_params,
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
        owner: MemoryScope | None = None,
    ) -> dict[str, Any]:
        """Archive (or DETACH DELETE) a node by ``(label, name|title, owner)``.

        The merge-key (``name`` vs ``title``) is selected from
        :data:`TITLE_KEYED_LABELS`. Names are only unique per owner, so
        ``owner`` pins the caller's own node. ``None`` targets the
        identity-less node.

        Returns a dict with:

        * ``matched`` (bool) — at least one node existed.
        * ``deleted`` (int) — DETACH DELETE count when ``purge=True``,
          ``0`` when archiving.
        """
        merge_key = "title" if label in TITLE_KEYED_LABELS else "name"
        owner_clause, owner_params = owner_filter_cypher(owner, "n")
        params = {"name": name, **owner_params}

        if purge:
            query = (
                f"MATCH (n:{label} {{{merge_key}: $name}}) WHERE {owner_clause} "
                "DETACH DELETE n "
                "RETURN count(*) AS deleted"
            )
            records = self._client.run(query, params)
            deleted = records[0]["deleted"] if records else 0
            return {"matched": deleted > 0, "deleted": deleted}

        query = (
            f"MATCH (n:{label} {{{merge_key}: $name}}) WHERE {owner_clause} "
            "SET n.status = 'archived', n.archived_at = datetime(), "
            "    n.archived_reason = 'forget' "
            "RETURN n"
        )
        records = self._client.run(query, params)
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
                "    n.archived_reason = 'ttl' "
                "RETURN count(n) AS affected"
            )

        records = self._client.run(query, {"days": days})
        affected = records[0]["affected"] if records else 0
        return {"affected": affected}

    # ------------------------------------------------------------------
    # Insight operations (skills/proactive.py + skills/reflect.py)
    # ------------------------------------------------------------------

    def get_pending_insights(
        self,
        limit: int = 10,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Pending Insights within ``scope``, ordered by confidence (highest
        first), breaking ties by ``created_at`` (newest first).

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → empty list.
        """
        scope_clause, scope_params = scope_filter_cypher(scope, "i")
        where_sql = f"AND {scope_clause} " if scope_clause else ""
        records = self._client.run(
            "MATCH (i:Insight {status: $status}) "
            f"WHERE i.source_query IS NOT NULL {where_sql}"
            "RETURN i.title AS title, i.body AS body, "
            "       i.confidence AS confidence, "
            "       i.source_query AS source_query, "
            "       i.created_at AS created_at "
            "ORDER BY i.confidence DESC, i.created_at DESC "
            "LIMIT $limit",
            {"status": "pending", "limit": limit, **scope_params},
        )
        return [dict(r) for r in records]

    def update_insight_status(
        self, title: str, new_status: str, scope: MemoryScope | None = None
    ) -> bool:
        """Set ``status`` and the matching timestamp (``approved_at`` /
        ``dismissed_at``) on an Insight node.

        Titles are templated and only unique per owner, so with ``scope``
        exactly one Insight visible in it is updated (own first).
        """
        ts_field = "approved_at" if new_status == "approved" else "dismissed_at"
        match, params = scoped_key_lookup("Insight", "title", scope, var="i")
        query = (
            f"{match} "
            f"SET i.status = $new_status, "
            f"    i.{ts_field} = datetime(), "
            "    i.updated_at = datetime() "
            "RETURN i.title AS title"
        )
        records = self._client.run(
            query,
            {"key_value": title, "new_status": new_status, **params},
        )
        return len(records) > 0

    def get_insight_by_title(
        self,
        title: str,
        scope: MemoryScope | None = None,
    ) -> dict[str, Any] | None:
        """Fetch an Insight by exact title, within ``scope``.

        Returns ``{status, body, confidence, source_query}`` or ``None``.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → ``None``.
        """
        scope_clause, scope_params = scope_filter_cypher(scope, "i")
        where_sql = f"AND {scope_clause} " if scope_clause else ""
        records = self._client.run(
            "MATCH (i:Insight {title: $title}) "
            f"WHERE 1=1 {where_sql}"
            "RETURN i.status AS status, i.body AS body, "
            "       i.confidence AS confidence, "
            "       i.source_query AS source_query",
            {"title": title, **scope_params},
        )
        if records:
            return dict(records[0])
        return None

    def mark_insight_synced(
        self, title: str, obsidian_path: str, scope: MemoryScope | None = None
    ) -> bool:
        """Set ``obsidian_path`` + ``synced_at`` + ``updated_at`` on an
        Insight node (scoped like :meth:`update_insight_status`).  Returns
        ``True`` if the Insight existed.
        """
        match, params = scoped_key_lookup("Insight", "title", scope, var="i")
        records = self._client.run(
            f"{match} "
            "SET i.obsidian_path = $path, "
            "    i.synced_at = datetime(), "
            "    i.updated_at = datetime() "
            "RETURN i.title AS title",
            {"key_value": title, "path": obsidian_path, **params},
        )
        return len(records) > 0

    def get_dismissed_insight_titles(
        self,
        scope: MemoryScope | None = None,
    ) -> set[str]:
        """Titles of dismissed Insights within ``scope``.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → empty set.
        """
        scope_clause, scope_params = scope_filter_cypher(scope, "i")
        where_sql = f"AND {scope_clause} " if scope_clause else ""
        records = self._client.run(
            "MATCH (i:Insight {status: 'dismissed'}) "
            f"WHERE 1=1 {where_sql}"
            "RETURN i.title AS title",
            scope_params,
        )
        return {r["title"] for r in records}

    def get_approved_insight_titles(
        self,
        scope: MemoryScope | None = None,
    ) -> set[str]:
        """Titles of approved Insights within ``scope``.

        Used by reflect to skip patterns the user has already approved.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → empty set.
        """
        scope_clause, scope_params = scope_filter_cypher(scope, "i")
        where_sql = f"AND {scope_clause} " if scope_clause else ""
        records = self._client.run(
            "MATCH (i:Insight {status: 'approved'}) "
            f"WHERE 1=1 {where_sql}"
            "RETURN i.title AS title",
            scope_params,
        )
        return {r["title"] for r in records}

    def find_insight_by_source_query(
        self,
        source_query: str,
        statuses: list[str] | None = None,
        scope: MemoryScope | None = None,
    ) -> dict[str, Any] | None:
        """Find an Insight by ``source_query`` and optional status filter,
        within ``scope``.

        Async-equivalent: :meth:`Neo4jAsyncStore.find_insight_by_source_query`.
        Default status set is ``["pending", "approved"]``.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → ``None``.
        """
        status_list = statuses or ["pending", "approved"]
        scope_clause, scope_params = scope_filter_cypher(scope, "i")
        where_sql = f"AND {scope_clause} " if scope_clause else ""
        records = self._client.run(
            "MATCH (i:Insight {source_query: $sq}) "
            f"WHERE i.status IN $statuses {where_sql}"
            "RETURN i.title AS title, i.status AS status LIMIT 1",
            {"sq": source_query, "statuses": status_list, **scope_params},
        )
        if records:
            return dict(records[0])
        return None

    # ------------------------------------------------------------------
    # Reflect — pattern detection (skills/reflect.py)
    # ------------------------------------------------------------------

    # Every detector restricts every matched node to the caller's scope
    # (Spec 001 FR-12); a ``None``/incomplete scope yields ``(false)`` per
    # alias and the query returns zero rows.

    def detect_cross_project_solutions(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Reflect detector, scoped to live nodes (``_reflect_cypher.cross_project_solutions``)."""
        cypher, params = _reflect_cypher.cross_project_solutions(scope)
        return [dict(r) for r in self._client.run(cypher, params)]

    def detect_shared_technology(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Reflect detector, scoped to live nodes (``_reflect_cypher.shared_technology``)."""
        cypher, params = _reflect_cypher.shared_technology(scope)
        return [dict(r) for r in self._client.run(cypher, params)]

    def detect_training_opportunities(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Reflect detector, scoped to live nodes (``_reflect_cypher.training_opportunities``)."""
        cypher, params = _reflect_cypher.training_opportunities(scope)
        return [dict(r) for r in self._client.run(cypher, params)]

    def detect_technique_transfer(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Reflect detector, scoped to live nodes (``_reflect_cypher.technique_transfer``)."""
        cypher, params = _reflect_cypher.technique_transfer(scope)
        return [dict(r) for r in self._client.run(cypher, params)]

    def detect_concept_clusters(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Reflect detector, scoped to live nodes (``_reflect_cypher.concept_clusters``)."""
        cypher, params = _reflect_cypher.concept_clusters(scope)
        return [dict(r) for r in self._client.run(cypher, params)]

    def detect_stale_knowledge(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Reflect detector, scoped to live nodes (``_reflect_cypher.stale_knowledge``)."""
        cypher, params = _reflect_cypher.stale_knowledge(scope)
        return [dict(r) for r in self._client.run(cypher, params)]

    def detect_hub_stubs(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Reflect detector, scoped to live nodes (``_reflect_cypher.hub_stubs``)."""
        cypher, params = _reflect_cypher.hub_stubs(scope)
        return [dict(r) for r in self._client.run(cypher, params)]

    def detect_under_connected_nodes(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Reflect detector, scoped to live nodes (``_reflect_cypher.under_connected_nodes``)."""
        cypher, params = _reflect_cypher.under_connected_nodes(scope)
        return [dict(r) for r in self._client.run(cypher, params)]

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
        ``archived_reason``) as the other soft-archive methods.  Returns
        ``True`` if a node was matched.
        """
        records = self._client.run(
            "MATCH (n {name: $name}) WHERE $label IN labels(n) "
            "SET n.status = 'archived', n.archived_at = datetime(), "
            "    n.archived_reason = 'missing_note' "
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

    def lookup_node_label(
        self,
        name: str,
        scope: MemoryScope | None = None,
    ) -> str | None:
        """Return the primary label of the node whose ``name`` (or
        ``title``, for nodes that use ``title`` instead of ``name`` such
        as ``Decision`` / ``Problem``) matches case-insensitively, within
        ``scope``.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → ``None``.
        """
        scope_clause, scope_params = scope_filter_cypher(scope, "n")
        where_sql = f"AND {scope_clause} " if scope_clause else ""
        records = self._client.run(
            "MATCH (n) WHERE toLower(COALESCE(n.name, n.title)) = toLower($name) "
            f"{where_sql}"
            "RETURN labels(n)[0] AS label LIMIT 1",
            {"name": name, **scope_params},
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

        With ``force=False`` returns nodes not yet labelled ``:Embedded``
        **or** explicitly flagged ``needs_reindex = true`` — the latter
        is set by the engine when an embedding round-trip returned a
        degenerate vector (issue #18). With ``force=True`` returns every
        node.

        Returns ``[{eid, labels, props}, ...]``.
        """
        records = self._client.run(
            "MATCH (n) WHERE "
            "NOT 'Embedded' IN labels(n) "
            "OR coalesce(n.needs_reindex, false) = true "
            "OR $force "
            "RETURN elementId(n) AS eid, labels(n) AS labels, "
            "properties(n) AS props",
            {"force": force},
        )
        return [dict(r) for r in records]
