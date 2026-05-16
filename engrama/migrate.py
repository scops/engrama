"""
Engrama — graph migration: NDJSON export / import.

Backend-agnostic dump and restore for the active ``GraphStore`` and
``VectorStore``. The on-disk format is **NDJSON** (one JSON object per
line) so the file streams, diffs, and can be filtered with ``jq``:

* Line 1 — envelope::

      {"engrama_export": 1, "version": "0.9.0",
       "exported_at": "...", "source_backend": "sqlite",
       "embedding_model": "...", "embedding_dimensions": 768}

* Subsequent lines — records, each tagged by ``type``::

      {"type": "node",     "label", "key_field", "key_value", "properties"}
      {"type": "relation", "from_label", "from_key", "from_value",
                           "rel_type", "to_label", "to_key", "to_value"}
      {"type": "vector",   "label", "key_field", "key_value", "vector"}

Cross-backend works because the factory keeps the contracts identical at
the boundary — exporter pulls through the ``iter_all_*`` migration
helpers (NOT in the ``GraphStore`` protocol because they only make
sense for bulk dump/restore), importer pushes through ``merge_node`` and
``merge_relation`` (which ARE in the protocol).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import engrama

EXPORT_FORMAT_VERSION = 1


def export_graph(
    graph_store: Any,
    vector_store: Any,
    output_path: Path,
    with_vectors: bool = True,
) -> dict[str, int]:
    """Stream ``graph_store`` + ``vector_store`` to ``output_path`` as NDJSON.

    Returns counts: ``{"nodes": N, "relations": N, "vectors": N}``.

    Vector export is skipped if the active vector store has
    ``dimensions == 0`` (i.e. no embedder was wired) or if
    ``with_vectors=False`` was requested explicitly.
    """
    backend = os.getenv("GRAPH_BACKEND", "sqlite")
    model = os.getenv("EMBEDDING_MODEL", "")
    dimensions = int(getattr(vector_store, "dimensions", 0) or 0)

    counts = {"nodes": 0, "relations": 0, "vectors": 0}
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        envelope = {
            "engrama_export": EXPORT_FORMAT_VERSION,
            "version": engrama.__version__,
            "exported_at": datetime.now(UTC).isoformat(),
            "source_backend": backend,
            "embedding_model": model,
            "embedding_dimensions": dimensions,
        }
        _write_line(f, envelope)

        for node in graph_store.iter_all_nodes():
            _write_line(f, {"type": "node", **node})
            counts["nodes"] += 1

        for rel in graph_store.iter_all_relations():
            _write_line(f, {"type": "relation", **rel})
            counts["relations"] += 1

        if with_vectors and dimensions > 0:
            for vec in vector_store.iter_all_vectors():
                _write_line(f, {"type": "vector", **vec})
                counts["vectors"] += 1

    return counts


def import_graph(
    graph_store: Any,
    vector_store: Any,
    input_path: Path,
    purge: bool = False,
) -> dict[str, int]:
    """Restore an NDJSON dump into the active ``graph_store`` and
    ``vector_store``. Returns counts:
    ``{"nodes": N, "relations": N, "vectors": N, "skipped_vectors": N}``.

    Vectors are only restored when the source's ``embedding_dimensions``
    matches the active vector store's. Mismatched vectors are counted
    under ``skipped_vectors`` and the user should run ``engrama reindex``
    after the import to rebuild embeddings under the active embedder.

    ``purge=True`` wipes the destination before importing (calls
    ``graph_store.purge_all()`` and ``vector_store.purge_all()``). The
    default is additive so import is safe on a populated graph.
    """
    counts = {"nodes": 0, "relations": 0, "vectors": 0, "skipped_vectors": 0}
    target_dims = int(getattr(vector_store, "dimensions", 0) or 0)

    if purge:
        graph_store.purge_all()
        if hasattr(vector_store, "purge_all"):
            vector_store.purge_all()

    with input_path.open("r", encoding="utf-8") as f:
        envelope_line = f.readline()
        if not envelope_line.strip():
            raise ValueError(f"{input_path} is empty")
        envelope = json.loads(envelope_line)
        fmt = envelope.get("engrama_export")
        if fmt != EXPORT_FORMAT_VERSION:
            raise ValueError(
                f"{input_path} has export format v{fmt}; this engrama "
                f"only reads v{EXPORT_FORMAT_VERSION}."
            )
        source_dims = int(envelope.get("embedding_dimensions") or 0)
        vector_dim_match = source_dims > 0 and source_dims == target_dims

        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rtype = rec.get("type")
            if rtype == "node":
                graph_store.merge_node(
                    rec["label"],
                    rec["key_field"],
                    rec["key_value"],
                    rec.get("properties", {}),
                )
                counts["nodes"] += 1
            elif rtype == "relation":
                graph_store.merge_relation(
                    rec["from_label"],
                    rec["from_key"],
                    rec["from_value"],
                    rec["rel_type"],
                    rec["to_label"],
                    rec["to_key"],
                    rec["to_value"],
                )
                counts["relations"] += 1
            elif rtype == "vector":
                if not vector_dim_match:
                    counts["skipped_vectors"] += 1
                    continue
                stored = vector_store.store_vector_by_key(
                    rec["label"],
                    rec["key_field"],
                    rec["key_value"],
                    rec["vector"],
                )
                if stored:
                    counts["vectors"] += 1
                else:
                    # Node not present yet — shouldn't happen on a well-
                    # formed dump because nodes come before vectors, but
                    # counts the gap honestly if it does.
                    counts["skipped_vectors"] += 1
            # Unknown record types are silently ignored — forward-
            # compatible: an older engrama can still read a newer dump
            # by skipping the records it doesn't understand.

    return counts


def _write_line(handle: Any, obj: dict[str, Any]) -> None:
    """Write one JSON object + newline. ``ensure_ascii=False`` so the
    file stays readable when the graph contains non-ASCII text.
    """
    handle.write(json.dumps(obj, ensure_ascii=False))
    handle.write("\n")


# ---------------------------------------------------------------------------
# Key-canonicalisation migration (#54)
# ---------------------------------------------------------------------------
#
# Pre-#53 the engine picked the merge key from whichever of ``name`` /
# ``title`` the caller had put in the property bag. Writes that used
# the wrong key for a title-keyed label (notably the MCP path, fixed
# in #59 follow-up) landed under the wrong column. The fix in #53
# stopped new writes from drifting, but the existing rows stayed
# misnamed. This migration walks every label in ``TITLE_KEYED_LABELS``
# (and ``Concept`` as the canonical name-keyed example covered by the
# symmetric direction), detects misnamed rows, and rewrites them to
# the canonical key. Idempotent — running twice is a no-op.


def _canonical_key_for_label(label: str) -> str:
    """Return ``"title"`` for labels in :data:`TITLE_KEYED_LABELS`,
    else ``"name"``."""
    from engrama.core.schema import TITLE_KEYED_LABELS

    return "title" if label in TITLE_KEYED_LABELS else "name"


def _all_known_labels() -> list[str]:
    """Every node label the schema knows about, in stable order. Used
    to scope a full migration sweep."""
    from engrama.core.schema import NodeType

    return sorted(member.value for member in NodeType)


def detect_misnamed_keys(
    graph_store: Any,
    *,
    labels: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return a plan of rows whose ``key_field`` doesn't match the
    canonical position in :data:`TITLE_KEYED_LABELS`.

    Each entry is shaped::

        {
            "label": "Experiment",
            "node_id": <backend-specific node id>,
            "current_key_field": "name",
            "canonical_key_field": "title",
            "key_value": "smoke-2026-05-15",
            "conflict": False,
            "conflict_reason": None,
        }

    On Neo4j, ``conflict=True`` means a sibling node with the same
    label already carries ``key_value`` under the canonical key, so a
    naive rename would violate the uniqueness constraint. The migrator
    skips those rows and reports them; resolve them manually before
    re-running.

    On SQLite, conflicts cannot occur because the ``UNIQUE(label,
    key_value)`` row constraint already collapses the two writes onto
    a single row at write time — so the migration is always a
    rename-in-place there.
    """
    labels = list(labels) if labels else _all_known_labels()
    backend = type(graph_store).__name__
    plan: list[dict[str, Any]] = []
    for label in labels:
        canonical = _canonical_key_for_label(label)
        if backend == "SqliteGraphStore":
            plan.extend(_detect_sqlite(graph_store, label, canonical))
        elif backend == "Neo4jGraphStore":
            plan.extend(_detect_neo4j(graph_store, label, canonical))
        else:
            raise NotImplementedError(f"migrate_keys does not support backend {backend!r} yet")
    return plan


def migrate_keys(
    graph_store: Any,
    *,
    labels: list[str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Detect (and optionally apply) the key-canonicalisation migration.

    Default is a dry-run: returns the plan and what each entry *would*
    do, with no writes. Pass ``apply=True`` to actually rewrite the
    rows.

    Returns a summary dict::

        {
            "dry_run": bool,
            "scanned_labels": [...],
            "plan": [<entry>, ...],
            "renamed": int,
            "skipped_conflict": int,
            "errors": [str, ...],
        }
    """
    labels = list(labels) if labels else _all_known_labels()
    plan = detect_misnamed_keys(graph_store, labels=labels)
    summary: dict[str, Any] = {
        "dry_run": not apply,
        "scanned_labels": labels,
        "plan": plan,
        "renamed": 0,
        "skipped_conflict": 0,
        "errors": [],
    }
    if not apply:
        # Without --apply, classify each entry without writing.
        summary["renamed"] = sum(1 for e in plan if not e.get("conflict"))
        summary["skipped_conflict"] = sum(1 for e in plan if e.get("conflict"))
        return summary

    backend = type(graph_store).__name__
    for entry in plan:
        try:
            if entry.get("conflict"):
                summary["skipped_conflict"] += 1
                continue
            if backend == "SqliteGraphStore":
                _apply_sqlite(graph_store, entry)
            elif backend == "Neo4jGraphStore":
                _apply_neo4j(graph_store, entry)
            summary["renamed"] += 1
        except Exception as e:
            summary["errors"].append(f"{entry['label']} {entry['key_value']!r}: {e}")
    return summary


# ---- SQLite-specific helpers ----


def _detect_sqlite(store: Any, label: str, canonical: str) -> list[dict[str, Any]]:
    cur = store._conn.execute(
        "SELECT id, key_field, key_value FROM nodes WHERE label = ? AND key_field != ?",
        (label, canonical),
    )
    return [
        {
            "label": label,
            "node_id": row["id"],
            "current_key_field": row["key_field"],
            "canonical_key_field": canonical,
            "key_value": row["key_value"],
            "conflict": False,
            "conflict_reason": None,
        }
        for row in cur.fetchall()
    ]


def _apply_sqlite(store: Any, entry: dict[str, Any]) -> None:
    """Rewrite a row's ``key_field`` and re-stamp the canonical key in
    its ``props`` blob.

    SQLite's ``UNIQUE(label, key_value)`` constraint makes the
    rename-in-place safe: there cannot be a sibling row that would
    collide, so we never need to merge two rows on this backend.
    """
    canonical = entry["canonical_key_field"]
    alias = entry["current_key_field"]
    value = entry["key_value"]
    cur = store._conn.execute("SELECT props FROM nodes WHERE id = ?", (entry["node_id"],))
    row = cur.fetchone()
    if row is None:
        return
    props = json.loads(row["props"] or "{}")
    props[canonical] = value
    # Drop the alias key only when it carries the same identity value;
    # an alias that points elsewhere is a different problem and gets
    # preserved verbatim for manual cleanup.
    if props.get(alias) == value:
        props.pop(alias, None)
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    now = _datetime.now(_UTC).isoformat()
    store._conn.execute(
        "UPDATE nodes SET key_field = ?, props = ?, updated_at = ? WHERE id = ?",
        (canonical, json.dumps(props), now, entry["node_id"]),
    )
    # Keep FTS in sync (the row may have had props.name in its
    # searchable text; canonicalising swaps it for props.title).
    sync_fts = getattr(store, "_sync_fts", None)
    if callable(sync_fts):
        sync_fts(entry["node_id"], props)
    store._conn.commit()


# ---- Neo4j-specific helpers ----


def _detect_neo4j(store: Any, label: str, canonical: str) -> list[dict[str, Any]]:
    """Find nodes for ``label`` carrying the alias property but missing
    the canonical one.

    Conflict detection: if the same identity already exists on a
    sibling node under the canonical key, mark the entry as a
    conflict so :func:`migrate_keys` can skip-and-report instead of
    triggering a uniqueness violation on apply.
    """
    alias = "name" if canonical == "title" else "title"
    client = store._client
    query = (
        f"MATCH (n:{label}) "
        f"WHERE n.{alias} IS NOT NULL AND n.{canonical} IS NULL "
        f"RETURN elementId(n) AS node_id, n.{alias} AS alias_value"
    )
    rows = client.run(query)
    out: list[dict[str, Any]] = []
    for r in rows:
        value = r["alias_value"]
        # Check for a sibling that already has the canonical key set.
        check_query = (
            f"MATCH (other:{label}) "
            f"WHERE other.{canonical} = $value AND elementId(other) <> $node_id "
            "RETURN elementId(other) AS other_id"
        )
        sibling = client.run(check_query, {"value": value, "node_id": r["node_id"]})
        conflict = bool(sibling)
        conflict_reason = (
            f"another {label} node already carries {canonical}={value!r} "
            f"(elementId={sibling[0]['other_id']}) — merge manually before retry"
            if conflict
            else None
        )
        out.append(
            {
                "label": label,
                "node_id": r["node_id"],
                "current_key_field": alias,
                "canonical_key_field": canonical,
                "key_value": value,
                "conflict": conflict,
                "conflict_reason": conflict_reason,
            }
        )
    return out


def _apply_neo4j(store: Any, entry: dict[str, Any]) -> None:
    label = entry["label"]
    canonical = entry["canonical_key_field"]
    alias = entry["current_key_field"]
    node_id = entry["node_id"]
    query = (
        f"MATCH (n:{label}) WHERE elementId(n) = $node_id "
        f"SET n.{canonical} = n.{alias}, n.updated_at = datetime() "
        f"REMOVE n.{alias}"
    )
    store._client.run(query, {"node_id": node_id})
