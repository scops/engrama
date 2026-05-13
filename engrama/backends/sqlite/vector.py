"""
Engrama — SQLite vector store via the sqlite-vec extension.

``SqliteVecStore`` shares a connection with :class:`SqliteGraphStore`
so embeddings live in the same database file. The vec0 virtual table
is created lazily because vec0 requires the dimension at CREATE time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
from typing import Any

logger = logging.getLogger("engrama.backends.sqlite.vector")


def _pack(embedding: list[float]) -> bytes:
    return struct.pack(f"<{len(embedding)}f", *embedding)


class SqliteVecStore:
    """``VectorStore`` backed by sqlite-vec's ``vec0`` virtual table.

    Parameters:
        conn: An open ``sqlite3.Connection`` (typically reused from the
            adjacent :class:`SqliteGraphStore` so both layers see the
            same data file).
        dimensions: Embedding dimensionality. When ``0`` every operation
            is a silent no-op — matches ``NullVectorStore`` semantics so
            the engine can be wired with a dimensionless embedder.
        index_name: Name of the vec0 virtual table.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        dimensions: int,
        index_name: str = "node_embeddings",
    ) -> None:
        self._conn = conn
        self._dimensions = int(dimensions)
        self._index_name = index_name
        self._index_ready = False

    # ------------------------------------------------------------------
    # Properties / lifecycle
    # ------------------------------------------------------------------

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def ensure_index(self) -> None:
        """Create the vec0 virtual table if it doesn't exist yet.

        Idempotent. Called by the factory after the embedder reports its
        dimensions; safe to call multiple times.
        """
        if self._index_ready or self._dimensions == 0:
            self._index_ready = True
            return
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._index_name} "
            f"USING vec0(node_id INTEGER PRIMARY KEY, "
            f"embedding FLOAT[{self._dimensions}])"
        )
        self._conn.commit()
        self._index_ready = True

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def store_vectors(
        self,
        items: list[tuple[str, list[float]]],
    ) -> int:
        """Bulk store embeddings keyed by node id (string of nodes.id).

        vec0 virtual tables don't support ``ON CONFLICT`` — we delete
        any prior row for each id then insert the new vector. SQLite
        runs both in the same transaction so a partial overwrite is
        impossible.
        """
        if self._dimensions == 0 or not items:
            return 0
        self.ensure_index()
        rows = [(int(nid), _pack(vec)) for nid, vec in items]
        ids = [(r[0],) for r in rows]
        self._conn.executemany(
            f"DELETE FROM {self._index_name} WHERE node_id = ?",
            ids,
        )
        self._conn.executemany(
            f"INSERT INTO {self._index_name}(node_id, embedding) VALUES (?, ?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def store_vector_by_key(
        self,
        label: str,
        key_field: str,
        key_value: str,
        embedding: list[float],
    ) -> bool:
        """Engine convenience: look up the node by ``(label, key_value)``
        and store the embedding against its id.
        """
        if self._dimensions == 0:
            return False
        cur = self._conn.execute(
            "SELECT id FROM nodes WHERE label = ? AND key_value = ?",
            (label, key_value),
        )
        row = cur.fetchone()
        if row is None:
            return False
        self.store_vectors(
            [(str(row[0] if not isinstance(row, sqlite3.Row) else row["id"]), embedding)]
        )
        return True

    def delete_vectors(self, node_ids: list[str]) -> int:
        if self._dimensions == 0 or not node_ids or not self._index_ready:
            return 0
        ids = [(int(n),) for n in node_ids]
        self._conn.executemany(
            f"DELETE FROM {self._index_name} WHERE node_id = ?",
            ids,
        )
        self._conn.commit()
        return len(ids)

    def purge_all(self) -> None:
        """Drop every stored vector. Migration-only; pairs with
        :meth:`SqliteGraphStore.purge_all` for ``engrama import --purge``.
        """
        if not self._index_ready and self._dimensions == 0:
            return
        # vec0 virtual tables don't support TRUNCATE; bulk DELETE is fine.
        self._conn.execute(f"DELETE FROM {self._index_name}")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def iter_all_vectors(self):
        """Yield ``{label, key_field, key_value, vector}`` for every stored
        embedding, resolved against the nodes table so the dump is
        portable across backends.
        """
        if self._dimensions == 0 or not self._index_ready:
            return
        cur = self._conn.execute(
            f"""
            SELECT n.label, n.key_field, n.key_value, v.embedding
              FROM {self._index_name} v
              JOIN nodes n ON n.id = v.node_id
             ORDER BY n.id
            """
        )
        # vec0 stores vectors as little-endian float32 blobs.
        fmt = f"<{self._dimensions}f"
        for row in cur:
            blob = row["embedding"]
            yield {
                "label": row["label"],
                "key_field": row["key_field"],
                "key_value": row["key_value"],
                "vector": list(struct.unpack(fmt, blob)),
            }

    def search_vectors(
        self,
        query_embedding: list[float],
        limit: int = 10,
        scope: Any = None,  # MemoryScope placeholder, unused for now
    ) -> list[dict[str, Any]]:
        """k-ANN cosine search. Returns ``[{node_id, score, label, key}]``.

        ``score`` is ``1 - distance`` so larger = better, matching the
        Neo4j vector-index convention.
        """
        if self._dimensions == 0 or not self._index_ready:
            return []
        try:
            cur = self._conn.execute(
                f"""
                SELECT v.node_id                                AS node_id,
                       1 - v.distance                           AS score,
                       n.label                                  AS label,
                       n.key_value                              AS key,
                       json_extract(n.props, '$.summary')       AS summary,
                       json_extract(n.props, '$.description')   AS description,
                       json_extract(n.props, '$.tags')          AS tags,
                       json_extract(n.props, '$.confidence')    AS confidence,
                       n.updated_at                             AS updated_at
                FROM {self._index_name} v
                JOIN nodes n ON n.id = v.node_id
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance
                """,
                (_pack(query_embedding), limit),
            )
        except sqlite3.OperationalError as e:
            logger.warning("vec0 search failed: %s", e)
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
                    "node_id": str(r["node_id"]),
                    "score": r["score"],
                    "label": r["label"],
                    "key": r["key"],
                    "summary": r["summary"] or r["description"] or "",
                    "tags": tags,
                    "confidence": r["confidence"],
                    "updated_at": r["updated_at"],
                }
            )
        return results

    def search_similar(
        self,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Alias for :meth:`search_vectors` matching Neo4jAsyncStore."""
        return self.search_vectors(query_embedding, limit=limit)

    def count(self) -> int:
        if self._dimensions == 0 or not self._index_ready:
            return 0
        cur = self._conn.execute(f"SELECT COUNT(*) AS n FROM {self._index_name}")
        return cur.fetchone()["n"]

    def count_embeddings(self) -> int:
        """Alias for :meth:`count` matching Neo4jAsyncStore."""
        return self.count()
