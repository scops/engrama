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
import sqlite3
from pathlib import Path
from typing import Any

import sqlite_vec

logger = logging.getLogger("engrama.backends.sqlite")

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_FTS_FIELDS = (
    "name", "title", "description", "notes", "rationale",
    "solution", "context", "body", "summary", "tags",
)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _fts_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return str(v)


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
            "SELECT id, props, created_at FROM nodes "
            "WHERE label = ? AND key_value = ?",
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
            self._conn.execute(
                "UPDATE nodes SET props = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged), now, node_id),
            )
            final_props = merged

        self._sync_fts(node_id, final_props)
        self._conn.commit()
        return [{
            "n": {
                "_id": str(node_id),
                "_labels": [label],
                **final_props,
                "created_at": created_at,
                "updated_at": now,
            }
        }]

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
    ) -> dict[str, Any]:
        """Archive a node by its name (or title). Returns shape used by
        the forget skill: ``{"archived": bool, "node": {...} | None}``.
        """
        # Try name-keyed first, then title-keyed.
        cur = self._conn.execute(
            "SELECT id, props, label, key_field, key_value FROM nodes "
            "WHERE label = ? AND key_value = ?",
            (label, name),
        )
        row = cur.fetchone()
        if row is None:
            return {"archived": False, "node": None}
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
        return {
            "archived": True,
            "node": {
                "label": row["label"],
                "key": row["key_field"],
                "name": row["key_value"],
                "archived_at": now,
            },
        }

    def list_existing_nodes(self, limit: int = 200) -> list[dict[str, str]]:
        cur = self._conn.execute(
            "SELECT label, key_value AS name FROM nodes "
            "ORDER BY key_value LIMIT ?",
            (limit,),
        )
        return [{"label": r["label"], "name": r["name"]} for r in cur.fetchall()]

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
        """Idempotent relationship insert. Silently no-op if an endpoint
        does not exist (mirrors Neo4j's MATCH-then-MERGE behaviour).
        """
        cur = self._conn.execute(
            "SELECT id FROM nodes WHERE label = ? AND key_value = ?",
            (from_label, from_value),
        )
        from_row = cur.fetchone()
        cur = self._conn.execute(
            "SELECT id FROM nodes WHERE label = ? AND key_value = ?",
            (to_label, to_value),
        )
        to_row = cur.fetchone()
        if from_row is None or to_row is None:
            return []
        now = _now_iso()
        self._conn.execute(
            "INSERT OR IGNORE INTO edges(from_id, rel_type, to_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (from_row["id"], rel_type, to_row["id"], now),
        )
        self._conn.commit()
        return [{"rel_type": rel_type}]

    def get_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return rows of ``{"start", "rel", "neighbour"}`` dicts.

        ``rel`` is a list of edge-dicts traversed (length == path depth).
        Walks edges in both directions to mirror Neo4j's undirected
        ``-[r*1..N]-`` pattern. Depth limited by *hops*.
        """
        cur = self._conn.execute(
            "SELECT id FROM nodes WHERE label = ? AND key_value = ?",
            (label, key_value),
        )
        start_row = cur.fetchone()
        if start_row is None:
            return []
        start_id = start_row["id"]
        # Recursive CTE walks both directions; we serialise rel ids and
        # types into JSON arrays so we can rebuild the rel chain client-side.
        cur = self._conn.execute(
            """
            WITH RECURSIVE walk(start_id, current_id, depth, rel_ids, rel_types) AS (
                SELECT ?, ?, 0, json('[]'), json('[]')
                UNION ALL
                SELECT w.start_id, e.to_id, w.depth + 1,
                       json_insert(w.rel_ids,   '$[#]', e.id),
                       json_insert(w.rel_types, '$[#]', e.rel_type)
                FROM walk w JOIN edges e ON e.from_id = w.current_id
                WHERE w.depth < ?
                UNION ALL
                SELECT w.start_id, e.from_id, w.depth + 1,
                       json_insert(w.rel_ids,   '$[#]', e.id),
                       json_insert(w.rel_types, '$[#]', e.rel_type)
                FROM walk w JOIN edges e ON e.to_id = w.current_id
                WHERE w.depth < ?
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
            ORDER BY walk.depth
            LIMIT ?
            """,
            (start_id, start_id, hops, hops, limit),
        )
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
            results.append({
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
            })
        return results

    def get_node_with_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
    ) -> dict[str, Any] | None:
        """Convenience: ``{node, neighbours}`` shape used by MCP context."""
        node = self.get_node(label, key_field, key_value)
        if node is None:
            return None
        # Strip embedding-like blobs and timestamps from neighbour props
        # to keep the response compact (mirrors Neo4j async behaviour).
        rows = self.get_neighbours(label, key_field, key_value, hops=hops)
        seen: set[tuple[str, str]] = set()
        neighbours: list[dict[str, Any]] = []
        for row in rows:
            n = row["neighbour"]
            n_label = n.get("_labels", ["Unknown"])[0]
            n_name = n.get("name") or n.get("title")
            if not n_name or (n_label, n_name) in seen:
                continue
            seen.add((n_label, n_name))
            neighbours.append({
                "label": n_label,
                "name": n_name,
                "via": list(dict.fromkeys(r["_type"] for r in row["rel"])),
                "properties": {
                    k: v for k, v in n.items()
                    if not k.startswith("_")
                    and k not in {"created_at", "updated_at", "details", "embedding"}
                },
            })
        # The root node also strips embedding for response compactness.
        root_node = {
            k: v for k, v in node.items() if k != "embedding"
        }
        return {"node": root_node, "neighbours": neighbours}

    def lookup_node_label(self, name: str) -> str | None:
        """Find a node by name OR title (case-insensitive). First match wins."""
        cur = self._conn.execute(
            "SELECT label FROM nodes WHERE LOWER(key_value) = LOWER(?) LIMIT 1",
            (name,),
        )
        row = cur.fetchone()
        return row["label"] if row else None

    def count_labels(self) -> dict[str, int]:
        """Per-label node count. Excludes Insight nodes (mirrors Neo4j)."""
        cur = self._conn.execute(
            "SELECT label, COUNT(*) AS n FROM nodes "
            "WHERE label != 'Insight' "
            "GROUP BY label ORDER BY n DESC",
        )
        return {r["label"]: r["n"] for r in cur.fetchall()}

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def fulltext_search(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """FTS5 search across the searchable text fields. Returns the
        same shape as the Neo4j store: ``[{type, name, score, summary,
        tags, confidence, updated_at}]``.

        ``score`` is the negated FTS5 ``rank`` so larger = better match.
        """
        if not query.strip():
            return []
        try:
            cur = self._conn.execute(
                """
                SELECT n.label                                  AS type,
                       n.key_value                              AS name,
                       -nodes_fts.rank                          AS score,
                       json_extract(n.props, '$.summary')       AS summary,
                       json_extract(n.props, '$.description')   AS description,
                       json_extract(n.props, '$.tags')          AS tags,
                       json_extract(n.props, '$.confidence')    AS confidence,
                       n.updated_at                             AS updated_at
                FROM nodes_fts
                JOIN nodes n ON n.id = nodes_fts.rowid
                WHERE nodes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            )
        except sqlite3.OperationalError as e:
            # FTS5 syntax errors on caller queries (e.g. unbalanced quotes)
            # — return empty rather than propagating, matching how Neo4j
            # silently returns no matches for a bad Lucene string.
            logger.debug("FTS5 query failed for %r: %s", query, e)
            return []
        results: list[dict[str, Any]] = []
        for r in cur.fetchall():
            tags = r["tags"]
            if tags:
                try:
                    tags = json.loads(tags)
                except (TypeError, ValueError):
                    tags = None
            results.append({
                "type": r["type"],
                "name": r["name"],
                "score": r["score"],
                "summary": r["summary"] or r["description"] or "",
                "tags": tags,
                "confidence": r["confidence"],
                "updated_at": r["updated_at"],
            })
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
                    "SELECT props FROM nodes WHERE id = ?", (node_id,),
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
            cutoff = (
                _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=max_age_days)
            ).isoformat()
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
        cutoff = (
            _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)
        ).isoformat()
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
                    f"DELETE FROM nodes WHERE id IN ({placeholders})", ids,
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

    def get_pending_insights(self, limit: int = 10) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT key_value AS title,
                   json_extract(props, '$.body')         AS body,
                   json_extract(props, '$.confidence')   AS confidence,
                   json_extract(props, '$.source_query') AS source_query,
                   created_at
            FROM nodes
            WHERE label = 'Insight'
              AND json_extract(props, '$.status') = 'pending'
            ORDER BY confidence DESC, created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    def update_insight_status(self, title: str, new_status: str) -> bool:
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

    def get_insight_by_title(self, title: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            """
            SELECT json_extract(props, '$.status')       AS status,
                   json_extract(props, '$.body')         AS body,
                   json_extract(props, '$.confidence')   AS confidence,
                   json_extract(props, '$.source_query') AS source_query
            FROM nodes
            WHERE label = 'Insight' AND key_value = ?
            """,
            (title,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def mark_insight_synced(self, title: str, obsidian_path: str) -> bool:
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

    def get_dismissed_insight_titles(self) -> set[str]:
        cur = self._conn.execute(
            "SELECT key_value FROM nodes WHERE label = 'Insight' "
            "AND json_extract(props, '$.status') = 'dismissed'",
        )
        return {r["key_value"] for r in cur.fetchall()}

    def find_insight_by_source_query(
        self,
        source_query: str,
        statuses: list[str] | None = None,
    ) -> dict[str, Any] | None:
        status_list = statuses or ["pending", "approved"]
        placeholders = ", ".join(["?"] * len(status_list))
        cur = self._conn.execute(
            f"""
            SELECT key_value                            AS title,
                   json_extract(props, '$.status')      AS status
            FROM nodes
            WHERE label = 'Insight'
              AND json_extract(props, '$.source_query') = ?
              AND json_extract(props, '$.status') IN ({placeholders})
            LIMIT 1
            """,
            (source_query, *status_list),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Reflect — pattern detection (skills/reflect.py)
    # ------------------------------------------------------------------

    def detect_cross_project_solutions(self) -> list[dict[str, Any]]:
        """Open Problem in project B shares a Concept with a resolved
        Problem in project A that has a Decision.

        Returns rows ``{target_project, open_problem, decision,
        source_project, concept}``.
        """
        cur = self._conn.execute(
            """
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
            WHERE pB.label = 'Project' AND pA.id != pB.id
            """,
        )
        return [dict(r) for r in cur.fetchall()]

    def detect_shared_technology(self) -> list[dict[str, Any]]:
        """Two distinct entities both connect to the same Technology via
        USES / TEACHES / COMPOSED_OF.
        """
        cur = self._conn.execute(
            """
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
              AND a.label != 'Insight' AND b.label != 'Insight'
            """,
        )
        return [dict(r) for r in cur.fetchall()]

    def detect_training_opportunities(self) -> list[dict[str, Any]]:
        """Vulnerability or open Problem shares a Concept with a Course."""
        cur = self._conn.execute(
            """
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
            WHERE issue.label = 'Vulnerability'
               OR (issue.label = 'Problem'
                   AND json_extract(issue.props, '$.status') = 'open')
            """,
        )
        return [dict(r) for r in cur.fetchall()]

    def detect_technique_transfer(self) -> list[dict[str, Any]]:
        """Technique used in domain A could apply in domain B because
        another entity in B shares a Concept with the technique.
        """
        cur = self._conn.execute(
            """
            WITH technique_in_domain AS (
                SELECT t.id AS t_id, t.key_value AS t_name,
                       d.id AS d_id, d.key_value AS d_name
                FROM nodes t
                JOIN edges e ON e.from_id = t.id AND e.rel_type = 'IN_DOMAIN'
                JOIN nodes d ON d.id = e.to_id AND d.label = 'Domain'
                WHERE t.label = 'Technique'
            ),
            technique_concept AS (
                SELECT t.id AS t_id, c.id AS c_id
                FROM nodes t
                JOIN edges e ON e.from_id = t.id
                            AND e.rel_type IN ('INSTANCE_OF', 'APPLIES')
                JOIN nodes c ON c.id = e.to_id AND c.label = 'Concept'
                WHERE t.label = 'Technique'
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
            )
            GROUP BY tid.t_id, tid.d_id, d2.id
            ORDER BY related_entities DESC
            LIMIT 10
            """,
        )
        return [dict(r) for r in cur.fetchall()]

    def detect_concept_clusters(self) -> list[dict[str, Any]]:
        """Concept connected to >= 3 entities via INSTANCE_OF/APPLIES.

        Returns ``{concept, entity_count, sample}`` with ``sample`` a
        list of up to 5 ``{name, label}`` dicts (mirrors Neo4j
        ``connected[..5]``).
        """
        cur = self._conn.execute(
            """
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
            WHERE c.label = 'Concept'
            GROUP BY c.id
            HAVING COUNT(DISTINCT n.id) >= 3
            ORDER BY entity_count DESC
            LIMIT 10
            """,
        )
        results = []
        for r in cur.fetchall():
            sample = json.loads(r["sample_raw"])[:5]
            results.append({
                "concept": r["concept"],
                "entity_count": r["entity_count"],
                "sample": sample,
            })
        return results

    def detect_stale_knowledge(self) -> list[dict[str, Any]]:
        """Nodes >=90d stale or with confidence <0.3 connected to an
        active Project or Course.
        """
        cutoff = (
            _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=90)
        ).isoformat()
        # Edges in either direction connect n to active. We UNION ALL
        # the two directions so SQLite can use the indexes on each side.
        cur = self._conn.execute(
            """
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
                        n.updated_at < ?
                     OR (json_extract(n.props, '$.confidence') IS NOT NULL
                         AND CAST(json_extract(n.props, '$.confidence') AS REAL) < 0.3)
                      )
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
                        n.updated_at < ?
                     OR (json_extract(n.props, '$.confidence') IS NOT NULL
                         AND CAST(json_extract(n.props, '$.confidence') AS REAL) < 0.3)
                      )
            )
            ORDER BY COALESCE(CAST(confidence AS REAL), 1.0) ASC, last_updated ASC
            LIMIT 15
            """,
            (cutoff, cutoff),
        )
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

    def detect_under_connected_nodes(self) -> list[dict[str, Any]]:
        """Nodes with fewer than 2 relationships (excluding Domain/Insight
        and archived).
        """
        cur = self._conn.execute(
            """
            SELECT
                n.key_value AS name,
                n.label     AS label,
                (
                    SELECT COUNT(*) FROM edges e
                    WHERE e.from_id = n.id OR e.to_id = n.id
                ) AS rel_count,
                n.created_at AS created
            FROM nodes n
            WHERE n.label NOT IN ('Domain', 'Insight')
              AND COALESCE(json_extract(n.props, '$.status'), '') != 'archived'
            GROUP BY n.id
            HAVING rel_count < 2
            ORDER BY n.created_at DESC
            LIMIT 15
            """,
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Obsidian helpers (adapters/obsidian/sync.py + skills/associate.py)
    # ------------------------------------------------------------------

    def find_obsidian_path(self, label: str, name: str) -> str | None:
        cur = self._conn.execute(
            "SELECT json_extract(props, '$.obsidian_path') AS path "
            "FROM nodes WHERE label = ? AND key_value = ?",
            (label, name),
        )
        row = cur.fetchone()
        return row["path"] if row and row["path"] else None

    def list_documented_nodes(self) -> list[dict[str, Any]]:
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
            from_label, "name", from_name,
            "LINKS_TO",
            to_label, "name", to_name,
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
        cur = self._conn.execute(
            "SELECT label, key_value FROM nodes "
            "WHERE LOWER(key_value) = LOWER(?) LIMIT 1",
            (target_name,),
        )
        row = cur.fetchone()
        if row is not None:
            self.merge_relation(
                from_label, "name", from_name,
                "LINKS_TO",
                row["label"], "name", row["key_value"],
            )
        return 1

    # ------------------------------------------------------------------
    # CLI helpers (engrama/cli.py)
    # ------------------------------------------------------------------

    def apply_schema_statements(
        self, statements: list[str],
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
        self, concept_name: str, domain_name: str,
    ) -> None:
        self.merge_node("Concept", "name", concept_name, {})
        self.merge_relation(
            "Concept", "name", concept_name,
            "IN_DOMAIN",
            "Domain", "name", domain_name,
        )

    def list_nodes_for_embedding(
        self, force: bool = False,
    ) -> list[dict[str, Any]]:
        """Return nodes that need embeddings.

        Once the vector store is wired, this filters by absence of an
        entry in ``node_embeddings``. For now we return every node
        (force=True) or every node missing an embedding flag in props.
        """
        if force:
            cur = self._conn.execute(
                "SELECT id, label, props FROM nodes"
            )
        else:
            cur = self._conn.execute(
                "SELECT id, label, props FROM nodes "
                "WHERE COALESCE(json_extract(props, '$.embedded'), 0) = 0"
            )
        return [
            {
                "eid": str(r["id"]),
                "labels": [r["label"]],
                "props": json.loads(r["props"]) if r["props"] else {},
            }
            for r in cur.fetchall()
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sync_fts(self, node_id: int, props: dict[str, Any]) -> None:
        """Mirror node text fields into the FTS5 index."""
        self._conn.execute("DELETE FROM nodes_fts WHERE rowid = ?", (node_id,))
        cols = ", ".join(_FTS_FIELDS)
        placeholders = ", ".join(["?"] * len(_FTS_FIELDS))
        values = [_fts_value(props.get(f)) for f in _FTS_FIELDS]
        self._conn.execute(
            f"INSERT INTO nodes_fts(rowid, {cols}) VALUES (?, {placeholders})",
            [node_id, *values],
        )
