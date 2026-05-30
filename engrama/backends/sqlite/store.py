"""
Engrama — SQLite graph store (sync).

Implements the ``GraphStore`` protocol on top of stdlib ``sqlite3`` plus
``sqlite_vec`` for vector similarity. Schema lives in ``schema.sql``.

The store carries a single connection. Threading: ``check_same_thread``
is disabled so the async wrapper (:class:`SqliteAsyncStore`) can call
methods through ``asyncio.to_thread`` without copying connections.
SQLite serialises writes internally; readers run concurrently.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import sqlite_vec

from engrama.core.scope import MemoryScope, scope_filter_sql

logger = logging.getLogger("engrama.backends.sqlite")

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_FTS_FIELDS = (
    "name",
    "title",
    "description",
    "notes",
    "rationale",
    "solution",
    "context",
    "body",
    "summary",
    "tags",
)

# A token is "safe" to pass to FTS5 MATCH unquoted iff it contains only
# the bareword alphabet. Everything else (hyphens, colons, parentheses,
# quotes, wildcards, ...) is grammar to FTS5 and must be quoted.
_FTS5_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_]+$")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _fts_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return str(v)


def _sanitize_fts5_query(query: str) -> str:
    """Turn a free-form user query into a syntactically valid FTS5 MATCH
    expression.

    FTS5's default tokenizer treats characters like ``-``, ``:``, ``(``,
    ``"`` and ``*`` as grammar. Passing raw user input therefore makes
    common queries (e.g. ``engrama-mcp-server``) either error out with
    ``OperationalError`` or miss the fulltext path silently.

    The strategy is deliberately conservative: split on whitespace and
    wrap any token that contains anything outside ``[A-Za-z0-9_]`` as a
    quoted phrase, doubling any embedded ``"`` per the FTS5 grammar.
    Pure-alphanumeric tokens — including the operator keywords ``AND``,
    ``OR``, ``NOT`` and ``NEAR`` — pass through unchanged, so callers
    that intentionally use boolean syntax keep their semantics.
    """
    tokens = query.split()
    out: list[str] = []
    for tok in tokens:
        if _FTS5_SAFE_TOKEN.fullmatch(tok):
            out.append(tok)
        else:
            escaped = tok.replace('"', '""')
            out.append(f'"{escaped}"')
    return " ".join(out)


def _node_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Build the protocol-shaped node dict from a nodes row."""
    props = json.loads(row["props"]) if row["props"] else {}
    return {
        "_id": str(row["id"]),
        "_labels": [row["label"]],
        **props,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class SqliteGraphStore:
    """Sync ``GraphStore`` (and partial ``VectorStore``) backed by SQLite.

    Parameters:
        path: Filesystem path to the database file. Use ``:memory:`` for
            ephemeral storage. Parent directory is created if needed.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.enable_load_extension(True)
        try:
            sqlite_vec.load(self._conn)
        finally:
            self._conn.enable_load_extension(False)
        self._init_schema_from_file()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def _init_schema_from_file(self) -> None:
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            self._conn.executescript(f.read())
        # CREATE TABLE IF NOT EXISTS doesn't add columns to a pre-existing
        # table, so pre-Spec-001 DBs need idempotent ALTERs to gain the
        # new ``edges.org_id`` / ``edges.user_id`` columns. SQLite raises
        # "duplicate column name" if the column already exists, which we
        # swallow so the call is safe on every connection.
        for stmt in (
            "ALTER TABLE edges ADD COLUMN org_id TEXT",
            "ALTER TABLE edges ADD COLUMN user_id TEXT",
        ):
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_scope ON edges(org_id, user_id)")
        self._conn.commit()

    def init_schema(self, schema: Any = None) -> None:
        """No-op: schema is applied at connection time. Kept for protocol parity."""
        return None

    def health_check(self) -> dict[str, Any]:
        cur = self._conn.execute("SELECT sqlite_version() AS v")
        version = cur.fetchone()["v"]
        cur = self._conn.execute("SELECT count(*) AS n FROM nodes")
        node_count = cur.fetchone()["n"]
        return {
            "ok": True,
            "backend": "sqlite",
            "sqlite_version": version,
            "path": self._path,
            "node_count": node_count,
        }

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
        """Create or update a node. Returns ``[{"n": <node-dict>}]``.

        Mirrors the Neo4j store's contract: ``created_at`` is set once;
        ``updated_at`` refreshes on every write. ``valid_from`` and
        ``confidence`` default on CREATE only; on MATCH they're preserved
        unless the caller supplies new values. ``valid_to``, when present
        on the existing node and not in the update, is cleared (revival).
        """
        now = _now_iso()
        properties = dict(properties)  # don't mutate caller
        # Embedding is handled by the vector store layer; ignore here.
        properties.pop("_id", None)
        properties.pop("_labels", None)

        cur = self._conn.execute(
            "SELECT id, props, created_at FROM nodes WHERE label = ? AND key_value = ?",
            (label, key_value),
        )
        row = cur.fetchone()

        if row is None:
            # CREATE
            full = dict(properties)
            full[key_field] = key_value
            full.setdefault("valid_from", properties.get("valid_from") or now)
            full.setdefault(
                "confidence",
                properties["confidence"] if "confidence" in properties else 1.0,
            )
            # Stable node identity (#6): mint a UUID unless the caller adopted
            # one (e.g. an existing Obsidian note's id). Mirrors the Neo4j store.
            full.setdefault("engrama_id", str(uuid.uuid4()))
            cur = self._conn.execute(
                "INSERT INTO nodes(label, key_field, key_value, props, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (label, key_field, key_value, json.dumps(full), now, now),
            )
            node_id = cur.lastrowid
            created_at = now
            final_props = full
        else:
            # MATCH
            node_id = row["id"]
            created_at = row["created_at"]
            existing = json.loads(row["props"]) if row["props"] else {}
            merged = dict(existing)
            for k, v in properties.items():
                merged[k] = v
            # Revival: clear valid_to unless caller explicitly set one.
            if "valid_to" not in properties:
                merged.pop("valid_to", None)
            # Stable identity (#6): an existing id always wins (a caller can't
            # rewrite it); backfill nodes written before this field existed.
            if existing.get("engrama_id"):
                merged["engrama_id"] = existing["engrama_id"]
            elif not merged.get("engrama_id"):
                merged["engrama_id"] = str(uuid.uuid4())
            self._conn.execute(
                "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged), now, node_id),
            )
            final_props = merged

        self._sync_fts(node_id, final_props)
        self._conn.commit()
        return [
            {
                "n": {
                    "_id": str(node_id),
                    "_labels": [label],
                    **final_props,
                    "created_at": created_at,
                    "updated_at": now,
                }
            }
        ]

    def get_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
    ) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT * FROM nodes WHERE label = ? AND key_value = ?",
            (label, key_value),
        )
        row = cur.fetchone()
        if row is None:
            return None
        props = json.loads(row["props"]) if row["props"] else {}
        return {
            **props,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def delete_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        soft: bool = True,
    ) -> bool:
        cur = self._conn.execute(
            "SELECT id, props FROM nodes WHERE label = ? AND key_value = ?",
            (label, key_value),
        )
        row = cur.fetchone()
        if row is None:
            return False
        if soft:
            now = _now_iso()
            props = json.loads(row["props"]) if row["props"] else {}
            props["status"] = "archived"
            props["archived_at"] = now
            self._conn.execute(
                "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
                (json.dumps(props), now, row["id"]),
            )
            self._sync_fts(row["id"], props)
        else:
            self._conn.execute("DELETE FROM nodes WHERE id = ?", (row["id"],))
            self._conn.execute("DELETE FROM nodes_fts WHERE rowid = ?", (row["id"],))
        self._conn.commit()
        return True

    def archive_node_by_name(
        self,
        label: str,
        name: str,
        *,
        purge: bool = False,
    ) -> dict[str, Any]:
        """Archive (or hard-delete) a node by ``(label, name|title)``.

        Returns ``{"matched": bool, "deleted": int}`` to mirror the
        Neo4j store's contract — used by the forget skill.
        """
        cur = self._conn.execute(
            "SELECT id FROM nodes WHERE label = ? AND key_value = ?",
            (label, name),
        )
        row = cur.fetchone()
        if row is None:
            return {"matched": False, "deleted": 0}
        node_id = row["id"]
        if purge:
            self._conn.execute("DELETE FROM nodes_fts WHERE rowid = ?", (node_id,))
            self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            self._conn.commit()
            return {"matched": True, "deleted": 1}
        # Soft-archive
        now = _now_iso()
        cur = self._conn.execute(
            "SELECT props FROM nodes WHERE id = ?",
            (node_id,),
        )
        props = json.loads(cur.fetchone()["props"] or "{}")
        props["status"] = "archived"
        props["archived_at"] = now
        self._conn.execute(
            "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
            (json.dumps(props), now, node_id),
        )
        self._sync_fts(node_id, props)
        self._conn.commit()
        return {"matched": True, "deleted": 0}

    def list_existing_nodes(
        self,
        limit: int = 200,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, str]]:
        """List existing nodes within ``scope`` for ingest deduplication.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → empty list.
        """
        scope_clause, scope_params = scope_filter_sql(scope, "nodes", json_column="props")
        sql = "SELECT label, key_value AS name FROM nodes"
        params: dict[str, Any] = {"limit": limit}
        if scope_clause:
            sql += f" WHERE {scope_clause}"
            params.update(scope_params)
        sql += " ORDER BY key_value LIMIT :limit"
        cur = self._conn.execute(sql, params)
        return [{"label": r["label"], "name": r["name"]} for r in cur.fetchall()]

    def iter_all_nodes(self) -> Iterator[dict[str, Any]]:
        """Yield every node in the graph for export. Migration-only — not
        intended for query paths, which should use indexed lookups instead.
        """
        cur = self._conn.execute("SELECT label, key_field, key_value, props FROM nodes ORDER BY id")
        for row in cur:
            yield {
                "label": row["label"],
                "key_field": row["key_field"],
                "key_value": row["key_value"],
                "properties": json.loads(row["props"]) if row["props"] else {},
            }

    def purge_all(self) -> None:
        """Wipe every node, edge, and FTS row. Used by ``engrama import
        --purge``. Vectors live in a separate vec0 table and must be
        purged by :meth:`SqliteVecStore.purge_all`.
        """
        self._conn.execute("DELETE FROM edges")
        self._conn.execute("DELETE FROM nodes")
        self._conn.execute("DELETE FROM nodes_fts")
        self._conn.commit()

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
        """Idempotent relationship insert. Silently no-op if an endpoint
        does not exist (mirrors Neo4j's MATCH-then-MERGE behaviour).

        Endpoints are matched case-insensitively on ``key_value`` — the
        same way ``lookup_node_label`` resolves them — so lookup and merge
        can't disagree and silently drop an edge to a node that does exist
        (#93, mode 2). When ``scope`` is supplied, BOTH endpoints are also
        scope-filtered, closing the asymmetry that let an unscoped merge
        reach another tenant's nodes.

        Spec 001 FR-1: when ``scope`` is supplied, the writer's
        ``(org_id, user_id)`` is persisted on the edge so a future
        relation-scoped read can filter without re-walking endpoints. On
        an existing row, the scope is refreshed via ``ON CONFLICT … DO
        UPDATE`` so a tenant re-asserting a relation reclaims it
        idempotently. ``scope=None`` keeps the legacy unscoped path
        intact for callers (export/import, migration) that don't have an
        identity.
        """
        node_clause, node_params = "", {}
        if scope is not None and scope.org_id and scope.user_id:
            node_clause, node_params = scope_filter_sql(scope, "nodes", json_column="props")
        # Match endpoints case-insensitively (mirrors lookup_node_label) and,
        # when scoped, within the caller's tenant. Same SQL for both ends.
        node_sql = "SELECT id FROM nodes WHERE label = :label AND LOWER(key_value) = LOWER(:val)"
        if node_clause:
            node_sql += f" AND {node_clause}"
        node_sql += " LIMIT 1"
        from_row = self._conn.execute(
            node_sql, {"label": from_label, "val": from_value, **node_params}
        ).fetchone()
        to_row = self._conn.execute(
            node_sql, {"label": to_label, "val": to_value, **node_params}
        ).fetchone()
        if from_row is None or to_row is None:
            return []
        now = _now_iso()
        org_id = scope.org_id if scope is not None else None
        user_id = scope.user_id if scope is not None else None
        self._conn.execute(
            "INSERT INTO edges(from_id, rel_type, to_id, created_at, org_id, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(from_id, rel_type, to_id) DO UPDATE SET "
            "    org_id = COALESCE(excluded.org_id, edges.org_id), "
            "    user_id = COALESCE(excluded.user_id, edges.user_id)",
            (from_row["id"], rel_type, to_row["id"], now, org_id, user_id),
        )
        self._conn.commit()
        return [{"rel_type": rel_type}]

    def iter_all_relations(self) -> Iterator[dict[str, Any]]:
        """Yield every edge in the graph for export, resolved to the
        label/key tuple on each endpoint so the dump is portable across
        backends (Neo4j has no concept of our integer ``nodes.id``).
        """
        cur = self._conn.execute(
            """
            SELECT f.label     AS from_label,
                   f.key_field AS from_key,
                   f.key_value AS from_value,
                   e.rel_type  AS rel_type,
                   t.label     AS to_label,
                   t.key_field AS to_key,
                   t.key_value AS to_value
              FROM edges e
              JOIN nodes f ON f.id = e.from_id
              JOIN nodes t ON t.id = e.to_id
             ORDER BY e.id
            """
        )
        for row in cur:
            yield {
                "from_label": row["from_label"],
                "from_key": row["from_key"],
                "from_value": row["from_value"],
                "rel_type": row["rel_type"],
                "to_label": row["to_label"],
                "to_key": row["to_key"],
                "to_value": row["to_value"],
            }

    def get_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
        limit: int = 50,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Return rows of ``{"start", "rel", "neighbour"}`` dicts.

        ``rel`` is a list of edge-dicts traversed (length == path depth).
        Walks edges in both directions to mirror Neo4j's undirected
        ``-[r*1..N]-`` pattern. Depth limited by *hops*.

        DDR-003 Phase F: ``scope`` filters both the start node and each
        returned neighbour. A caller that can't see the start node gets
        an empty list, and neighbours outside the scope are excluded.
        Intermediate hops are not filtered — only the edge ids/types
        cross the boundary, not node data.
        """
        scope_clause, scope_params = scope_filter_sql(scope, "n", json_column="props")
        # Start-node lookup: also enforce scope so a caller can't reach
        # someone else's node by guessing its (label, key_value).
        start_scope_clause, _ = scope_filter_sql(scope, "n", json_column="props")
        if start_scope_clause:
            start_sql = (
                "SELECT id FROM nodes n "
                "WHERE n.label = :label AND n.key_value = :key_value AND " + start_scope_clause
            )
        else:
            start_sql = "SELECT id FROM nodes n WHERE n.label = :label AND n.key_value = :key_value"
        start_params: dict[str, Any] = {
            "label": label,
            "key_value": key_value,
            **scope_params,
        }
        cur = self._conn.execute(start_sql, start_params)
        start_row = cur.fetchone()
        if start_row is None:
            return []
        start_id = start_row["id"]
        # Recursive CTE walks both directions; we serialise rel ids and
        # types into JSON arrays so we can rebuild the rel chain client-side.
        sql = """
            WITH RECURSIVE walk(start_id, current_id, depth, rel_ids, rel_types) AS (
                SELECT :start_id, :start_id, 0, json('[]'), json('[]')
                UNION ALL
                SELECT w.start_id, e.to_id, w.depth + 1,
                       json_insert(w.rel_ids,   '$[#]', e.id),
                       json_insert(w.rel_types, '$[#]', e.rel_type)
                FROM walk w JOIN edges e ON e.from_id = w.current_id
                WHERE w.depth < :hops
                UNION ALL
                SELECT w.start_id, e.from_id, w.depth + 1,
                       json_insert(w.rel_ids,   '$[#]', e.id),
                       json_insert(w.rel_types, '$[#]', e.rel_type)
                FROM walk w JOIN edges e ON e.to_id = w.current_id
                WHERE w.depth < :hops
            )
            SELECT s.id          AS start_id,
                   s.label       AS start_label,
                   s.props       AS start_props,
                   s.created_at  AS start_created,
                   s.updated_at  AS start_updated,
                   n.id          AS neighbour_id,
                   n.label       AS neighbour_label,
                   n.props       AS neighbour_props,
                   n.created_at  AS neighbour_created,
                   n.updated_at  AS neighbour_updated,
                   walk.rel_ids,
                   walk.rel_types,
                   walk.depth
            FROM walk
            JOIN nodes s ON s.id = walk.start_id
            JOIN nodes n ON n.id = walk.current_id
            WHERE walk.depth > 0 AND n.id != walk.start_id
        """
        if scope_clause:
            sql += f" AND {scope_clause}"
        sql += " ORDER BY walk.depth LIMIT :limit"
        params: dict[str, Any] = {
            "start_id": start_id,
            "hops": hops,
            "limit": limit,
            **scope_params,
        }
        cur = self._conn.execute(sql, params)
        results: list[dict[str, Any]] = []
        for r in cur.fetchall():
            start_props = json.loads(r["start_props"]) if r["start_props"] else {}
            n_props = json.loads(r["neighbour_props"]) if r["neighbour_props"] else {}
            rel_ids = json.loads(r["rel_ids"])
            rel_types = json.loads(r["rel_types"])
            rels = [
                {"_id": str(rid), "_type": rtype}
                for rid, rtype in zip(rel_ids, rel_types, strict=True)
            ]
            results.append(
                {
                    "start": {
                        "_id": str(r["start_id"]),
                        "_labels": [r["start_label"]],
                        **start_props,
                        "created_at": r["start_created"],
                        "updated_at": r["start_updated"],
                    },
                    "rel": rels,
                    "neighbour": {
                        "_id": str(r["neighbour_id"]),
                        "_labels": [r["neighbour_label"]],
                        **n_props,
                        "created_at": r["neighbour_created"],
                        "updated_at": r["neighbour_updated"],
                    },
                }
            )
        return results

    def get_node_with_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
        scope: MemoryScope | None = None,
    ) -> dict[str, Any] | None:
        """Convenience: ``{node, neighbours}`` shape used by MCP context.

        DDR-003 Phase F: ``scope`` is forwarded to :meth:`get_neighbours`
        so traversal respects scope visibility. The root node lookup
        does *not* apply scope — admin/debug paths still need to fetch
        a known node by key.
        """
        node = self.get_node(label, key_field, key_value)
        if node is None:
            return None
        # Strip embedding-like blobs and timestamps from neighbour props
        # to keep the response compact (mirrors Neo4j async behaviour).
        rows = self.get_neighbours(label, key_field, key_value, hops=hops, scope=scope)
        seen: set[tuple[str, str]] = set()
        neighbours: list[dict[str, Any]] = []
        for row in rows:
            n = row["neighbour"]
            n_label = n.get("_labels", ["Unknown"])[0]
            n_name = n.get("name") or n.get("title")
            if not n_name or (n_label, n_name) in seen:
                continue
            seen.add((n_label, n_name))
            neighbours.append(
                {
                    "label": n_label,
                    "name": n_name,
                    "via": list(dict.fromkeys(r["_type"] for r in row["rel"])),
                    "properties": {
                        k: v
                        for k, v in n.items()
                        if not k.startswith("_")
                        and k not in {"created_at", "updated_at", "details", "embedding"}
                    },
                }
            )
        # The root node also strips embedding for response compactness.
        root_node = {k: v for k, v in node.items() if k != "embedding"}
        return {"node": root_node, "neighbours": neighbours}

    def lookup_node_label(
        self,
        name: str,
        scope: MemoryScope | None = None,
    ) -> str | None:
        """Find a node by name (case-insensitive) within ``scope``. First match wins.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → ``None``.
        """
        scope_clause, scope_params = scope_filter_sql(scope, "nodes", json_column="props")
        sql = "SELECT label FROM nodes WHERE LOWER(key_value) = LOWER(:name)"
        params: dict[str, Any] = {"name": name}
        if scope_clause:
            sql += f" AND {scope_clause}"
            params.update(scope_params)
        sql += " LIMIT 1"
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()
        return row["label"] if row else None

    def count_labels(
        self,
        scope: MemoryScope | None = None,
    ) -> dict[str, int]:
        """Per-label node count within ``scope``. Excludes Insight nodes (mirrors Neo4j).

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → empty dict.
        """
        scope_clause, scope_params = scope_filter_sql(scope, "nodes", json_column="props")
        sql = "SELECT label, COUNT(*) AS n FROM nodes WHERE label != 'Insight'"
        params: dict[str, Any] = {}
        if scope_clause:
            sql += f" AND {scope_clause}"
            params.update(scope_params)
        sql += " GROUP BY label ORDER BY n DESC"
        cur = self._conn.execute(sql, params)
        return {r["label"]: r["n"] for r in cur.fetchall()}

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def fulltext_search(
        self,
        query: str,
        limit: int = 10,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """FTS5 search across the searchable text fields. Returns the
        same shape as the Neo4j store: ``[{type, name, score, summary,
        tags, confidence, updated_at}]``.

        ``score`` is the negated FTS5 ``rank`` so larger = better match.

        DDR-003 Phase F: when ``scope`` is set, results are filtered by
        the scope-visibility rule (see :mod:`engrama.core.scope`).
        """
        if not query.strip():
            return []
        # Convert the raw user query into a valid FTS5 MATCH expression
        # so hyphens, colons and other tokenizer-significant characters
        # don't trip a syntax error or silently miss matches.
        match_expr = _sanitize_fts5_query(query)
        if not match_expr:
            return []
        scope_clause, scope_params = scope_filter_sql(scope, "n", json_column="props")
        sql = """
                SELECT n.label                                  AS type,
                       n.key_value                              AS name,
                       -nodes_fts.rank                          AS score,
                       json_extract(n.props, '$.summary')       AS summary,
                       json_extract(n.props, '$.description')   AS description,
                       json_extract(n.props, '$.tags')          AS tags,
                       json_extract(n.props, '$.confidence')    AS confidence,
                       json_extract(n.props, '$.trust_level')   AS trust_level,
                       n.updated_at                             AS updated_at
                FROM nodes_fts
                JOIN nodes n ON n.id = nodes_fts.rowid
                WHERE nodes_fts MATCH :match_expr
        """
        if scope_clause:
            sql += f" AND {scope_clause}"
        sql += " ORDER BY rank LIMIT :limit"
        params: dict[str, Any] = {"match_expr": match_expr, "limit": limit, **scope_params}
        try:
            cur = self._conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            # FTS5 syntax errors on caller queries (e.g. unbalanced quotes
            # that survive sanitization) — return empty rather than
            # propagating, matching how Neo4j silently returns no matches
            # for a bad Lucene string.
            logger.debug("FTS5 query failed for %r (sanitized to %r): %s", query, match_expr, e)
            return []
        results: list[dict[str, Any]] = []
        for r in cur.fetchall():
            tags = r["tags"]
            if tags:
                try:
                    tags = json.loads(tags)
                except (TypeError, ValueError):
                    tags = None
            results.append(
                {
                    "type": r["type"],
                    "name": r["name"],
                    "score": r["score"],
                    "summary": r["summary"] or r["description"] or "",
                    "tags": tags,
                    "confidence": r["confidence"],
                    "trust_level": r["trust_level"],
                    "updated_at": r["updated_at"],
                }
            )
        return results

    def run_cypher(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """SQLite has no Cypher. Raises so callers don't silently degrade."""
        raise NotImplementedError(
            "SQLite backend has no Cypher engine. Use named GraphStore methods "
            "(merge_node, fulltext_search, detect_*, etc.) instead."
        )

    # ------------------------------------------------------------------
    # Temporal operations
    # ------------------------------------------------------------------

    def expire_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
    ) -> bool:
        """Mark a node as no longer current (sets ``valid_to`` to now).

        Re-merging the node later clears ``valid_to`` (revival).
        """
        # scope-exempt: temporal write helper invoked by the SDK
        # `engine.expire_node` which the caller has already scoped; the
        # lookup-by-key here just resolves the row to update.
        cur = self._conn.execute(
            "SELECT id, props FROM nodes WHERE label = ? AND key_value = ?",
            (label, key_value),
        )
        row = cur.fetchone()
        if row is None:
            return False
        now = _now_iso()
        props = json.loads(row["props"]) if row["props"] else {}
        props["valid_to"] = now
        self._conn.execute(
            "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
            (json.dumps(props), now, row["id"]),
        )
        self._conn.commit()
        return True

    def decay_scores(
        self,
        rate: float = 0.01,
        min_confidence: float = 0.0,
        max_age_days: int = 0,
        label: str | None = None,
    ) -> dict[str, int]:
        """Apply exponential confidence decay, then optionally archive.

        Done in Python (fetch + recompute + write) because SQLite has no
        native ``exp``. For typical graphs (<100k nodes) this is fast
        enough; if it ever bites we'll move to a SQLite extension.
        """
        # scope-exempt: admin temporal-decay sweep — `engrama decay` runs
        # cross-tenant maintenance; a SaaS gateway should restrict
        # invocation. Matches Neo4j's `decay_confidence` exemption.
        import math

        label_filter = "AND label = ?" if label else ""
        params: tuple = (label,) if label else ()
        cur = self._conn.execute(
            f"SELECT id, props, updated_at FROM nodes "
            f"WHERE json_extract(props, '$.confidence') IS NOT NULL "
            f"  AND updated_at IS NOT NULL "
            f"  {label_filter}",
            params,
        )
        rows = cur.fetchall()

        now = _dt.datetime.now(_dt.UTC)
        decayed = 0
        updates: list[tuple[str, str, int]] = []
        for r in rows:
            try:
                ts = _dt.datetime.fromisoformat(r["updated_at"])
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.UTC)
            days_old = (now - ts).total_seconds() / 86400.0
            if days_old <= 0:
                continue
            props = json.loads(r["props"])
            old_conf = float(props.get("confidence", 1.0))
            new_conf = old_conf * math.exp(-rate * days_old)
            if new_conf == old_conf:
                continue
            props["confidence"] = new_conf
            updates.append((json.dumps(props), r["updated_at"], r["id"]))
            decayed += 1
        if updates:
            self._conn.executemany(
                "UPDATE nodes SET props = ? WHERE id = ?",
                [(p, i) for p, _, i in updates],
            )
            # We refresh the FTS index for the same rows because tags etc.
            # may include serialised confidence; cheap enough.
            for _, _, node_id in updates:
                self._conn.execute(
                    "SELECT props FROM nodes WHERE id = ?",
                    (node_id,),
                )
                # We don't bother updating FTS here — confidence isn't a
                # searchable field.
        archived = 0
        archive_now = _now_iso()
        if min_confidence > 0:
            cur = self._conn.execute(
                f"SELECT id, props FROM nodes WHERE "
                f"json_extract(props, '$.confidence') < ? "
                f"AND COALESCE(json_extract(props, '$.status'), '') != 'archived' "
                f"{label_filter}",
                (min_confidence, *params),
            )
            for r in cur.fetchall():
                props = json.loads(r["props"])
                props["status"] = "archived"
                props["archived_at"] = archive_now
                self._conn.execute(
                    "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(props), archive_now, r["id"]),
                )
                archived += 1
        if max_age_days > 0:
            cutoff = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=max_age_days)).isoformat()
            cur = self._conn.execute(
                f"SELECT id, props FROM nodes WHERE updated_at < ? "
                f"AND COALESCE(json_extract(props, '$.status'), '') != 'archived' "
                f"{label_filter}",
                (cutoff, *params),
            )
            for r in cur.fetchall():
                props = json.loads(r["props"])
                props["status"] = "archived"
                props["archived_at"] = archive_now
                self._conn.execute(
                    "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(props), archive_now, r["id"]),
                )
                archived += 1
        self._conn.commit()
        return {"decayed": decayed, "archived": archived}

    def query_at_date(
        self,
        date: str,
        label: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        # scope-exempt: admin temporal-view helper used by `engrama decay
        # --dry-run` to preview affected rows; the matching Neo4j method
        # carries the same exemption.
        """Return what was true at a given date (ISO string).

        Uses ISO lexicographic ordering — works because ISO timestamps
        sort the same as datetime values.
        """
        label_filter = "AND label = ?" if label else ""
        params: tuple = (date, date, *(label and (label,) or ()))
        cur = self._conn.execute(
            f"""
            SELECT label,
                   key_value AS name,
                   json_extract(props, '$.confidence') AS confidence,
                   json_extract(props, '$.valid_from') AS valid_from,
                   json_extract(props, '$.valid_to')   AS valid_to,
                   json_extract(props, '$.status')     AS status
            FROM nodes
            WHERE json_extract(props, '$.valid_from') IS NOT NULL
              AND json_extract(props, '$.valid_from') <= ?
              AND (json_extract(props, '$.valid_to') IS NULL
                   OR json_extract(props, '$.valid_to') >= ?)
              AND label NOT IN ('Insight', 'Domain')
              {label_filter}
            ORDER BY confidence DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def archive_nodes_older_than(
        self,
        label: str,
        days: int,
        *,
        purge: bool = False,
    ) -> dict[str, Any]:
        """Soft-archive (or DELETE) nodes whose ``updated_at`` is older
        than *days*. Returns ``{"affected": int}``.
        """
        # scope-exempt: admin TTL sweep — `engrama forget --ttl` runs
        # cross-tenant maintenance. SaaS surface should gate this at a
        # higher layer; OSS standalone is single-tenant so the unscoped
        # sweep matches user intent.
        cutoff = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "SELECT id, props FROM nodes "
            "WHERE label = ? AND updated_at < ? "
            "AND COALESCE(json_extract(props, '$.status'), '') != 'archived'",
            (label, cutoff),
        )
        rows = cur.fetchall()
        affected = 0
        if purge:
            ids = [r["id"] for r in rows]
            if ids:
                placeholders = ", ".join(["?"] * len(ids))
                self._conn.execute(
                    f"DELETE FROM nodes_fts WHERE rowid IN ({placeholders})",
                    ids,
                )
                self._conn.execute(
                    f"DELETE FROM nodes WHERE id IN ({placeholders})",
                    ids,
                )
                affected = len(ids)
        else:
            now = _now_iso()
            for r in rows:
                props = json.loads(r["props"])
                props["status"] = "archived"
                props["archived_at"] = now
                self._conn.execute(
                    "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(props), now, r["id"]),
                )
                affected += 1
        self._conn.commit()
        return {"affected": affected}

    # ------------------------------------------------------------------
    # Insight operations (skills/proactive.py + skills/reflect.py)
    # ------------------------------------------------------------------

    def get_pending_insights(
        self,
        limit: int = 10,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Pending Insights within ``scope``, ordered by confidence DESC then
        ``created_at`` DESC.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → empty list.
        """
        scope_clause, scope_params = scope_filter_sql(scope, "nodes", json_column="props")
        sql = """
            SELECT key_value AS title,
                   json_extract(props, '$.body')         AS body,
                   json_extract(props, '$.confidence')   AS confidence,
                   json_extract(props, '$.source_query') AS source_query,
                   created_at
            FROM nodes
            WHERE label = 'Insight'
              AND json_extract(props, '$.status') = 'pending'
        """
        params: dict[str, Any] = {"limit": limit}
        if scope_clause:
            sql += f"      AND {scope_clause}\n"
            params.update(scope_params)
        sql += "            ORDER BY confidence DESC, created_at DESC LIMIT :limit"
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def update_insight_status(self, title: str, new_status: str) -> bool:
        # scope-exempt: write path — the MCP `engrama_approve_insight` and SDK
        # `ProactiveSkill.approve/dismiss` first call `get_insight_by_title`
        # (scoped, fail-closed) to confirm ownership, then route here only
        # when the read returned a row. Cross-tenant promotion is therefore
        # blocked at the prior read.
        cur = self._conn.execute(
            "SELECT id, props FROM nodes WHERE label = 'Insight' AND key_value = ?",
            (title,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        now = _now_iso()
        props = json.loads(row["props"]) if row["props"] else {}
        props["status"] = new_status
        ts_field = "approved_at" if new_status == "approved" else "dismissed_at"
        props[ts_field] = now
        self._conn.execute(
            "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
            (json.dumps(props), now, row["id"]),
        )
        self._conn.commit()
        return True

    def get_insight_by_title(
        self,
        title: str,
        scope: MemoryScope | None = None,
    ) -> dict[str, Any] | None:
        """Fetch an Insight node by exact title, within ``scope``.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → ``None``.
        """
        scope_clause, scope_params = scope_filter_sql(scope, "nodes", json_column="props")
        sql = """
            SELECT json_extract(props, '$.status')       AS status,
                   json_extract(props, '$.body')         AS body,
                   json_extract(props, '$.confidence')   AS confidence,
                   json_extract(props, '$.source_query') AS source_query
            FROM nodes
            WHERE label = 'Insight' AND key_value = :title
        """
        params: dict[str, Any] = {"title": title}
        if scope_clause:
            sql += f"      AND {scope_clause}"
            params.update(scope_params)
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def mark_insight_synced(self, title: str, obsidian_path: str) -> bool:
        # scope-exempt: write path — same shape as `update_insight_status`.
        # `ProactiveSkill.write_to_vault` first reads the Insight via the
        # scoped `get_insight_by_title`, so cross-tenant marking is blocked
        # upstream.
        cur = self._conn.execute(
            "SELECT id, props FROM nodes WHERE label = 'Insight' AND key_value = ?",
            (title,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        now = _now_iso()
        props = json.loads(row["props"]) if row["props"] else {}
        props["obsidian_path"] = obsidian_path
        props["synced_at"] = now
        self._conn.execute(
            "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
            (json.dumps(props), now, row["id"]),
        )
        self._conn.commit()
        return True

    def get_dismissed_insight_titles(
        self,
        scope: MemoryScope | None = None,
    ) -> set[str]:
        """Titles of dismissed Insights within ``scope``.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → empty set.
        """
        scope_clause, scope_params = scope_filter_sql(scope, "nodes", json_column="props")
        sql = (
            "SELECT key_value FROM nodes WHERE label = 'Insight' "
            "AND json_extract(props, '$.status') = 'dismissed'"
        )
        params: dict[str, Any] = {}
        if scope_clause:
            sql += f" AND {scope_clause}"
            params.update(scope_params)
        cur = self._conn.execute(sql, params)
        return {r["key_value"] for r in cur.fetchall()}

    def get_approved_insight_titles(
        self,
        scope: MemoryScope | None = None,
    ) -> set[str]:
        """Titles of approved Insights within ``scope``.

        Used by reflect to skip patterns the user has already approved,
        so a re-run doesn't pin them back to ``status='pending'`` (the
        default applied by ``MERGE``).

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → empty set.
        """
        scope_clause, scope_params = scope_filter_sql(scope, "nodes", json_column="props")
        sql = (
            "SELECT key_value FROM nodes WHERE label = 'Insight' "
            "AND json_extract(props, '$.status') = 'approved'"
        )
        params: dict[str, Any] = {}
        if scope_clause:
            sql += f" AND {scope_clause}"
            params.update(scope_params)
        cur = self._conn.execute(sql, params)
        return {r["key_value"] for r in cur.fetchall()}

    def find_insight_by_source_query(
        self,
        source_query: str,
        statuses: list[str] | None = None,
        scope: MemoryScope | None = None,
    ) -> dict[str, Any] | None:
        """Find an Insight by ``source_query`` and status, within ``scope``.

        Spec 001: fail-closed — ``scope`` ``None``/incomplete → ``None``.
        """
        status_list = statuses or ["pending", "approved"]
        status_keys: list[str] = []
        params: dict[str, Any] = {"sq": source_query}
        for i, s in enumerate(status_list):
            k = f"st{i}"
            status_keys.append(f":{k}")
            params[k] = s
        scope_clause, scope_params = scope_filter_sql(scope, "nodes", json_column="props")
        sql = f"""
            SELECT key_value                            AS title,
                   json_extract(props, '$.status')      AS status
            FROM nodes
            WHERE label = 'Insight'
              AND json_extract(props, '$.source_query') = :sq
              AND json_extract(props, '$.status') IN ({", ".join(status_keys)})
        """
        if scope_clause:
            sql += f"      AND {scope_clause}\n"
            params.update(scope_params)
        sql += "            LIMIT 1"
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Reflect — pattern detection (skills/reflect.py)
    # ------------------------------------------------------------------
    #
    # Every detector restricts every joined ``nodes`` alias to the caller's
    # scope (Spec 001 FR-12). A ``None``/incomplete scope yields ``(1=0)``
    # from the helper for every alias, so the query short-circuits to zero
    # rows — reflect cannot leak patterns across tenants.

    @staticmethod
    def _scope_and(
        aliases: tuple[str, ...],
        scope: MemoryScope | None,
    ) -> tuple[str, dict[str, Any]]:
        """Build ``AND (scope_a) AND (scope_b)...`` SQL + params.

        Returns ``("", {})`` when ``scope`` is empty so the caller can
        unconditionally splice the fragment.
        """
        clauses: list[str] = []
        params: dict[str, Any] = {}
        for alias in aliases:
            clause, p = scope_filter_sql(scope, alias, json_column="props")
            if not clause:
                continue
            clauses.append(clause)
            params.update(p)
        if not clauses:
            return "", {}
        return " AND " + " AND ".join(clauses), params

    def detect_cross_project_solutions(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Open Problem in project B shares a Concept with a resolved
        Problem in project A that has a Decision, within ``scope``.

        Returns rows ``{target_project, open_problem, decision,
        source_project, concept}``.
        """
        scope_sql, scope_params = self._scope_and(("pB", "op", "c", "rp", "d", "pA"), scope)
        sql = f"""
            SELECT DISTINCT
                pB.key_value     AS target_project,
                op.key_value     AS open_problem,
                d.key_value      AS decision,
                pA.key_value     AS source_project,
                c.key_value      AS concept
            FROM nodes pB
            JOIN edges e1 ON e1.from_id = pB.id AND e1.rel_type = 'HAS'
            JOIN nodes op ON op.id = e1.to_id
                         AND op.label = 'Problem'
                         AND json_extract(op.props, '$.status') = 'open'
            JOIN edges e2 ON e2.from_id = op.id
                         AND e2.rel_type IN ('INSTANCE_OF', 'APPLIES')
            JOIN nodes c  ON c.id = e2.to_id AND c.label = 'Concept'
            JOIN edges e3 ON e3.to_id = c.id
                         AND e3.rel_type IN ('INSTANCE_OF', 'APPLIES')
            JOIN nodes rp ON rp.id = e3.from_id
                         AND rp.label = 'Problem'
                         AND json_extract(rp.props, '$.status') = 'resolved'
            JOIN edges e4 ON e4.from_id = rp.id AND e4.rel_type = 'SOLVED_BY'
            JOIN nodes d  ON d.id = e4.to_id AND d.label = 'Decision'
            JOIN edges e5 ON e5.to_id = d.id AND e5.rel_type = 'INFORMED_BY'
            JOIN nodes pA ON pA.id = e5.from_id AND pA.label = 'Project'
            WHERE pB.label = 'Project' AND pA.id != pB.id{scope_sql}
        """
        cur = self._conn.execute(sql, scope_params)
        return [dict(r) for r in cur.fetchall()]

    def detect_shared_technology(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Two distinct entities both connect to the same Technology via
        USES / TEACHES / COMPOSED_OF, within ``scope``.
        """
        scope_sql, scope_params = self._scope_and(("t", "a", "b"), scope)
        sql = f"""
            SELECT DISTINCT
                a.key_value AS entity_a, a.label AS type_a,
                b.key_value AS entity_b, b.label AS type_b,
                t.key_value AS technology
            FROM nodes t
            JOIN edges ea ON ea.to_id = t.id
                         AND ea.rel_type IN ('USES', 'TEACHES', 'COMPOSED_OF')
            JOIN nodes a  ON a.id = ea.from_id
            JOIN edges eb ON eb.to_id = t.id
                         AND eb.rel_type IN ('USES', 'TEACHES', 'COMPOSED_OF')
            JOIN nodes b  ON b.id = eb.from_id
            WHERE t.label = 'Technology'
              AND a.id < b.id
              AND a.label != 'Insight' AND b.label != 'Insight'{scope_sql}
        """
        cur = self._conn.execute(sql, scope_params)
        return [dict(r) for r in cur.fetchall()]

    def detect_training_opportunities(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Vulnerability or open Problem shares a Concept with a Course, within ``scope``."""
        scope_sql, scope_params = self._scope_and(("issue", "c", "course"), scope)
        sql = f"""
            SELECT DISTINCT
                issue.key_value AS issue,
                issue.label     AS issue_type,
                c.key_value     AS concept,
                course.key_value AS course
            FROM nodes issue
            JOIN edges e1 ON e1.from_id = issue.id
                         AND e1.rel_type IN ('INSTANCE_OF', 'APPLIES')
            JOIN nodes c  ON c.id = e1.to_id AND c.label = 'Concept'
            JOIN edges e2 ON e2.to_id = c.id AND e2.rel_type = 'COVERS'
            JOIN nodes course ON course.id = e2.from_id AND course.label = 'Course'
            WHERE (issue.label = 'Vulnerability'
               OR (issue.label = 'Problem'
                   AND json_extract(issue.props, '$.status') = 'open')){scope_sql}
        """
        cur = self._conn.execute(sql, scope_params)
        return [dict(r) for r in cur.fetchall()]

    def detect_technique_transfer(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Technique used in domain A could apply in domain B because
        another entity in B shares a Concept with the technique, within ``scope``.

        Scope is applied inside both CTEs (so the technique and its domain
        / concept all belong to the caller) and against the main-query
        aliases ``d2`` and ``other`` so the target domain and the related
        entity are also scoped.
        """
        # CTEs need their own scope clauses so the same MemoryScope is
        # threaded into ``t``, ``d``, ``c`` (CTE-internal) AND ``d2``,
        # ``other`` (main query). All five share the same scope params.
        cte_t, cte_t_params = self._scope_and(("t",), scope)
        cte_d, _ = self._scope_and(("d",), scope)
        cte_c, _ = self._scope_and(("c",), scope)
        main_scope_sql, main_params = self._scope_and(("d2", "other"), scope)
        # Param keys are identical across helpers (same scope), so merging
        # is idempotent.
        params: dict[str, Any] = {**cte_t_params, **main_params}
        sql = f"""
            WITH technique_in_domain AS (
                SELECT t.id AS t_id, t.key_value AS t_name,
                       d.id AS d_id, d.key_value AS d_name
                FROM nodes t
                JOIN edges e ON e.from_id = t.id AND e.rel_type = 'IN_DOMAIN'
                JOIN nodes d ON d.id = e.to_id AND d.label = 'Domain'
                WHERE t.label = 'Technique'{cte_t}{cte_d}
            ),
            technique_concept AS (
                SELECT t.id AS t_id, c.id AS c_id
                FROM nodes t
                JOIN edges e ON e.from_id = t.id
                            AND e.rel_type IN ('INSTANCE_OF', 'APPLIES')
                JOIN nodes c ON c.id = e.to_id AND c.label = 'Concept'
                WHERE t.label = 'Technique'{cte_t}{cte_c}
            )
            SELECT
                tid.t_name      AS technique,
                tid.d_name      AS source_domain,
                d2.key_value    AS target_domain,
                COUNT(DISTINCT other.id) AS related_entities
            FROM technique_in_domain tid
            JOIN nodes d2 ON d2.label = 'Domain' AND d2.id != tid.d_id
            JOIN edges eo ON eo.to_id = d2.id AND eo.rel_type = 'IN_DOMAIN'
            JOIN nodes other ON other.id = eo.from_id
            JOIN edges oc ON oc.from_id = other.id
                         AND oc.rel_type IN ('INSTANCE_OF', 'APPLIES')
            JOIN technique_concept tc ON tc.t_id = tid.t_id AND tc.c_id = oc.to_id
            WHERE NOT EXISTS (
                SELECT 1 FROM edges
                WHERE from_id = tid.t_id
                  AND rel_type = 'IN_DOMAIN'
                  AND to_id = d2.id
            ){main_scope_sql}
            GROUP BY tid.t_id, tid.d_id, d2.id
            ORDER BY related_entities DESC
            LIMIT 10
        """
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def detect_concept_clusters(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Concept connected to >= 3 entities via INSTANCE_OF/APPLIES, within ``scope``.

        Returns ``{concept, entity_count, sample}`` with ``sample`` a
        list of up to 5 ``{name, label}`` dicts (mirrors Neo4j
        ``connected[..5]``).
        """
        scope_sql, scope_params = self._scope_and(("c", "n"), scope)
        sql = f"""
            SELECT
                c.key_value AS concept,
                COUNT(DISTINCT n.id) AS entity_count,
                json_group_array(
                    DISTINCT json_object('name', n.key_value, 'label', n.label)
                ) AS sample_raw
            FROM nodes c
            JOIN edges e ON e.to_id = c.id
                        AND e.rel_type IN ('INSTANCE_OF', 'APPLIES')
            JOIN nodes n ON n.id = e.from_id
            WHERE c.label = 'Concept'{scope_sql}
            GROUP BY c.id
            HAVING COUNT(DISTINCT n.id) >= 3
            ORDER BY entity_count DESC
            LIMIT 10
        """
        cur = self._conn.execute(sql, scope_params)
        results = []
        for r in cur.fetchall():
            sample = json.loads(r["sample_raw"])[:5]
            results.append(
                {
                    "concept": r["concept"],
                    "entity_count": r["entity_count"],
                    "sample": sample,
                }
            )
        return results

    def detect_stale_knowledge(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Nodes >=90d stale or with confidence <0.3 connected to an
        active Project or Course, within ``scope``.
        """
        cutoff = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=90)).isoformat()
        # The UNION runs the same shape twice (one per edge direction); both
        # halves apply scope to ``n`` and ``active``. Same scope params are
        # reused across halves via named placeholders.
        scope_sql, scope_params = self._scope_and(("n", "active"), scope)
        params: dict[str, Any] = {"cutoff": cutoff, **scope_params}
        sql = f"""
            SELECT n_name, n_label, last_updated, confidence, project, rel
            FROM (
                SELECT
                    n.key_value AS n_name,
                    n.label     AS n_label,
                    n.updated_at AS last_updated,
                    json_extract(n.props, '$.confidence') AS confidence,
                    active.key_value AS project,
                    e.rel_type  AS rel,
                    n.id AS n_id
                FROM nodes n
                JOIN edges e ON e.from_id = n.id
                JOIN nodes active ON active.id = e.to_id
                                  AND active.label IN ('Project', 'Course')
                                  AND COALESCE(
                                        json_extract(active.props, '$.status'),
                                        'active'
                                      ) IN ('active', '')
                WHERE n.label NOT IN ('Project', 'Course', 'Domain', 'Insight')
                  AND (
                        n.updated_at < :cutoff
                     OR (json_extract(n.props, '$.confidence') IS NOT NULL
                         AND CAST(json_extract(n.props, '$.confidence') AS REAL) < 0.3)
                      ){scope_sql}
                UNION
                SELECT
                    n.key_value, n.label, n.updated_at,
                    json_extract(n.props, '$.confidence'),
                    active.key_value, e.rel_type, n.id
                FROM nodes n
                JOIN edges e ON e.to_id = n.id
                JOIN nodes active ON active.id = e.from_id
                                  AND active.label IN ('Project', 'Course')
                                  AND COALESCE(
                                        json_extract(active.props, '$.status'),
                                        'active'
                                      ) IN ('active', '')
                WHERE n.label NOT IN ('Project', 'Course', 'Domain', 'Insight')
                  AND (
                        n.updated_at < :cutoff
                     OR (json_extract(n.props, '$.confidence') IS NOT NULL
                         AND CAST(json_extract(n.props, '$.confidence') AS REAL) < 0.3)
                      ){scope_sql}
            )
            ORDER BY COALESCE(CAST(confidence AS REAL), 1.0) ASC, last_updated ASC
            LIMIT 15
        """
        cur = self._conn.execute(sql, params)
        return [
            {
                "name": r["n_name"],
                "label": r["n_label"],
                "last_updated": r["last_updated"],
                "confidence": r["confidence"],
                "project": r["project"],
                "rel": r["rel"],
            }
            for r in cur.fetchall()
        ]

    def detect_under_connected_nodes(
        self,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Nodes with fewer than 2 *substantive* relationships (excluding
        Domain/Insight and archived nodes), within ``scope``.

        Edges to neighbours marked ``status = 'stub'`` are not counted —
        stubs are placeholder nodes created during ingest before their
        real content arrives, and treating them as real connections
        masks genuinely under-connected nodes whose only neighbours are
        placeholders.
        """
        scope_sql, scope_params = self._scope_and(("n",), scope)
        sql = f"""
            SELECT
                n.key_value AS name,
                n.label     AS label,
                (
                    SELECT COUNT(*) FROM edges e
                    JOIN nodes m
                      ON m.id = CASE WHEN e.from_id = n.id
                                     THEN e.to_id
                                     ELSE e.from_id END
                    WHERE (e.from_id = n.id OR e.to_id = n.id)
                      AND COALESCE(json_extract(m.props, '$.status'), 'active')
                          != 'stub'
                ) AS rel_count,
                n.created_at AS created
            FROM nodes n
            WHERE n.label NOT IN ('Domain', 'Insight')
              AND COALESCE(json_extract(n.props, '$.status'), '') != 'archived'{scope_sql}
            GROUP BY n.id
            HAVING rel_count < 2
            ORDER BY n.created_at DESC
            LIMIT 15
        """
        cur = self._conn.execute(sql, scope_params)
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Obsidian helpers (adapters/obsidian/sync.py + skills/associate.py)
    # ------------------------------------------------------------------

    def find_obsidian_path(self, label: str, name: str) -> str | None:
        # scope-exempt: obsidian-sync internal — VAULT_PATH is per-deployment
        # (Engrama-owned), not per-tenant; the caller (`ObsidianSync`) drives
        # writes via the scoped engine. Surface this through a scoped wrapper
        # only when a SaaS deployment carries per-tenant vaults.
        cur = self._conn.execute(
            "SELECT json_extract(props, '$.obsidian_path') AS path "
            "FROM nodes WHERE label = ? AND key_value = ?",
            (label, name),
        )
        row = cur.fetchone()
        return row["path"] if row and row["path"] else None

    def list_documented_nodes(self) -> list[dict[str, Any]]:
        # scope-exempt: obsidian-sync internal listing for the
        # archive-missing pass; runs against the single Engrama-owned vault.
        cur = self._conn.execute(
            "SELECT label, key_value AS name, "
            "       json_extract(props, '$.obsidian_path') AS path "
            "FROM nodes WHERE json_extract(props, '$.obsidian_path') IS NOT NULL"
        )
        return [dict(r) for r in cur.fetchall()]

    def archive_node_for_missing_note(self, label: str, name: str) -> bool:
        """Like ``archive_node_by_name`` but used by the obsidian sync's
        archive-missing pass. Returns ``True`` if a node was matched.
        """
        # scope-exempt: obsidian-sync archive-missing write. Same vault
        # scope as `list_documented_nodes` above.
        cur = self._conn.execute(
            "SELECT id, props FROM nodes WHERE label = ? AND key_value = ?",
            (label, name),
        )
        row = cur.fetchone()
        if row is None:
            return False
        now = _now_iso()
        props = json.loads(row["props"]) if row["props"] else {}
        props["status"] = "archived"
        props["archived_at"] = now
        self._conn.execute(
            "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
            (json.dumps(props), now, row["id"]),
        )
        self._sync_fts(row["id"], props)
        self._conn.commit()
        return True

    def merge_wiki_link(
        self,
        *,
        from_label: str,
        from_name: str,
        to_label: str,
        to_name: str,
    ) -> None:
        """``MERGE (a)-[:LINKS_TO]->(b)`` — silent no-op if either endpoint
        is missing (ObsidianSync calls this for unresolved wiki-links).
        """
        self.merge_relation(
            from_label,
            "name",
            from_name,
            "LINKS_TO",
            to_label,
            "name",
            to_name,
        )

    def merge_wiki_link_by_target_name(
        self,
        *,
        from_label: str,
        from_name: str,
        target_name: str,
    ) -> int:
        """Resolve target by case-insensitive name and merge LINKS_TO.

        Returns ``1`` to mirror the unconditional counter in the Neo4j
        version (caller increments per call regardless of success).
        """
        # scope-exempt: obsidian-sync wiki-link resolution; relies on the
        # vault being single-tenant per deployment. A SaaS multi-tenant
        # variant should add a scoped lookup wrapper alongside this one.
        cur = self._conn.execute(
            "SELECT label, key_value FROM nodes WHERE LOWER(key_value) = LOWER(?) LIMIT 1",
            (target_name,),
        )
        row = cur.fetchone()
        if row is not None:
            self.merge_relation(
                from_label,
                "name",
                from_name,
                "LINKS_TO",
                row["label"],
                "name",
                row["key_value"],
            )
        return 1

    # ------------------------------------------------------------------
    # CLI helpers (engrama/cli.py)
    # ------------------------------------------------------------------

    def apply_schema_statements(
        self,
        statements: list[str],
    ) -> list[tuple[str, Exception]]:
        """Execute Cypher schema statements one at a time.

        SQLite cannot speak Cypher, so each statement is treated as a
        no-op and reported as a failure. The CLI ignores SQLite-side
        failures gracefully (the SQLite schema lives in schema.sql and
        is applied at connection time anyway).
        """
        return [
            (stmt, NotImplementedError("SQLite backend ignores Cypher schema"))
            for stmt in statements
        ]

    def seed_domain(self, name: str, description: str) -> None:
        self.merge_node("Domain", "name", name, {"description": description})

    def seed_concept_in_domain(
        self,
        concept_name: str,
        domain_name: str,
    ) -> None:
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
        """Return nodes that need embeddings.

        With ``force=True`` returns every node. With ``force=False``
        returns nodes missing an ``embedded`` marker or explicitly
        flagged ``needs_reindex = true`` — the latter is set by the
        engine when an embedding round-trip returned a degenerate
        vector (issue #18), so a follow-up ``engrama reindex`` heals
        them.
        """
        # scope-exempt: admin reindex backfill — same rationale as
        # `list_unembedded_nodes`. Embeddings stay on the source node so
        # this never relocates data across tenants.
        if force:
            cur = self._conn.execute("SELECT id, label, props FROM nodes")
        else:
            cur = self._conn.execute(
                "SELECT id, label, props FROM nodes "
                "WHERE COALESCE(json_extract(props, '$.embedded'), 0) = 0 "
                "   OR COALESCE(json_extract(props, '$.needs_reindex'), 0) = 1"
            )
        return [
            {
                "eid": str(r["id"]),
                "labels": [r["label"]],
                "props": json.loads(r["props"]) if r["props"] else {},
            }
            for r in cur.fetchall()
        ]

    def list_unembedded_nodes(
        self, limit: int = 100, scope: MemoryScope | None = None
    ) -> list[dict[str, Any]]:
        """Return nodes that carry no vector, newest-agnostic, capped at ``limit``.

        Source of truth is the presence of a row in the ``node_embeddings``
        vec0 table (mirrors the ``:Embedded`` label on Neo4j). A node whose
        embed-on-write failed has no vector row and shows up here. Backs the
        opportunistic sweep and ``engrama_reindex``. Returns
        ``{engrama_id, label, key_field, key_value, props}``.

        When ``scope`` is provided the scan is restricted to that tenant, so
        ``engrama_reindex`` (which passes a resolved scope) never reveals
        another tenant's node names. The internal opportunistic sweep and the
        admin CLI pass ``scope=None`` to keep the cross-tenant backfill.
        """
        # scope-exempt: ``scope=None`` is the admin reindex/sweep path —
        # backfills missing vectors across the whole graph; the embedding lives
        # on the same node it came from, so this never crosses tenant
        # boundaries on the data side. When a scope IS passed (the per-tenant
        # MCP reindex path) the scan is filtered below via scope_filter_sql.
        params: dict[str, Any] = {"limit": limit}
        scope_clause = ""
        if scope is not None:
            clause, scope_params = scope_filter_sql(scope, "n", json_column="props")
            scope_clause = f"AND {clause} "
            params.update(scope_params)
        try:
            cur = self._conn.execute(
                "SELECT n.label AS label, n.key_field AS key_field, "
                "       n.key_value AS key_value, n.props AS props "
                "FROM nodes n "
                "WHERE NOT EXISTS (SELECT 1 FROM node_embeddings v WHERE v.node_id = n.id) "
                f"{scope_clause}"
                "LIMIT :limit",
                params,
            )
        except sqlite3.OperationalError:
            # vec table never created (no embedder configured) → every node
            # is unembedded.
            fallback_clause = f"WHERE {clause} " if scope is not None else ""
            cur = self._conn.execute(
                "SELECT n.label AS label, n.key_field AS key_field, "
                "       n.key_value AS key_value, n.props AS props "
                f"FROM nodes n {fallback_clause}LIMIT :limit",
                params,
            )
        out: list[dict[str, Any]] = []
        for r in cur.fetchall():
            props = json.loads(r["props"]) if r["props"] else {}
            out.append(
                {
                    "engrama_id": props.get("engrama_id"),
                    "label": r["label"],
                    "key_field": r["key_field"],
                    "key_value": r["key_value"],
                    "props": props,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sync_fts(self, node_id: int, props: dict[str, Any]) -> None:
        """Mirror node text fields into the FTS5 index."""
        # scope-exempt: internal write-side helper — called after a node's
        # row has already been persisted (which the engine guard scoped).
        # The FTS rowid is the nodes.id, so this writes only to the row
        # the caller just touched.
        self._conn.execute("DELETE FROM nodes_fts WHERE rowid = ?", (node_id,))
        cols = ", ".join(_FTS_FIELDS)
        placeholders = ", ".join(["?"] * len(_FTS_FIELDS))
        values = [_fts_value(props.get(f)) for f in _FTS_FIELDS]
        self._conn.execute(
            f"INSERT INTO nodes_fts(rowid, {cols}) VALUES (?, {placeholders})",
            [node_id, *values],
        )
