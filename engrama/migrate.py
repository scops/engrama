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


# Temporal properties that must be Neo4j ``datetime`` values. A
# string-typed value here breaks ``duration.between(...)`` (decay,
# query_at_date). Kept local to migrate.py so the SQLite-only install
# path doesn't import the neo4j backend. See #76.
_TIMESTAMP_FIELDS: tuple[str, ...] = (
    "created_at",
    "updated_at",
    "valid_from",
    "valid_to",
    "archived_at",
    "synced_at",
    "approved_at",
    "dismissed_at",
    "decayed_at",
)


def migrate_timestamps(
    graph_store: Any,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Coerce string-typed temporal properties to Neo4j ``datetime`` (#76).

    Some nodes (notably anything restored via ``engrama import`` before
    the merge_node fix) carry ``updated_at`` / ``created_at`` etc. as
    ISO **strings** instead of Neo4j datetimes, which makes
    ``decay_scores`` and ``query_at_date`` fail the whole transaction on
    ``duration.between``. This rewrites those values in place, preserving
    the instant and only changing the type.

    Neo4j-only: SQLite stores timestamps as TEXT, so the type bug does
    not apply there and the migration is a no-op.

    Default is a dry-run that counts affected nodes per field. Pass
    ``apply=True`` to rewrite. Returns::

        {"dry_run": bool, "backend": str, "fields": {field: count},
         "fixed": int, "errors": [str], "skipped_reason": str | None}
    """
    backend = type(graph_store).__name__
    summary: dict[str, Any] = {
        "dry_run": not apply,
        "backend": backend,
        "fields": {},
        "fixed": 0,
        "errors": [],
        "skipped_reason": None,
    }
    if backend != "Neo4jGraphStore":
        summary["skipped_reason"] = (
            f"{backend} stores timestamps as text; no datetime coercion needed"
        )
        return summary

    client = graph_store._client
    for fld in _TIMESTAMP_FIELDS:
        try:
            count_q = (
                f"MATCH (n) WHERE n.{fld} IS NOT NULL "
                f"AND valueType(n.{fld}) STARTS WITH 'STRING' "
                "RETURN count(n) AS c"
            )
            rows = client.run(count_q)
            affected = rows[0]["c"] if rows else 0
            summary["fields"][fld] = affected
            if affected and apply:
                fix_q = (
                    f"MATCH (n) WHERE n.{fld} IS NOT NULL "
                    f"AND valueType(n.{fld}) STARTS WITH 'STRING' "
                    f"SET n.{fld} = datetime(n.{fld}) "
                    "RETURN count(n) AS c"
                )
                fixed_rows = client.run(fix_q)
                summary["fixed"] += fixed_rows[0]["c"] if fixed_rows else 0
        except Exception as e:
            summary["errors"].append(f"{fld}: {e}")
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


# ---------------------------------------------------------------------------
# Spec 001 T040 — tenancy backfill migration
# ---------------------------------------------------------------------------


def migrate_tenancy(
    graph_store: Any,
    owner_sub: str,
    *,
    dry_run: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Backfill ``(owner_sub, owner_sub)`` onto every identity-less row.

    A pre-Spec-001 graph carries no ``org_id`` / ``user_id`` on its nodes
    and edges. Under the fail-closed read filter every such row is
    invisible to every scope, so a fresh install of the new release would
    appear to have lost its data until this migration runs.

    Contract:

    * ``dry_run=True`` counts and samples what would change, but writes
      nothing. Safe to run repeatedly.
    * ``apply=True`` stamps ``(owner_sub, owner_sub)`` onto every node /
      relation that lacks identity, and purges nodes that are also missing
      their merge-key (pre-existing corruption, not real data).
    * Exactly one of ``dry_run`` or ``apply`` MUST be set.
    * Rows that already carry a different identity are left alone — they
      belong to a real tenant and overwriting them would be a leak.

    Returns a report dict suitable for the CLI: counts plus a small
    sample so the operator can sanity-check before re-running in apply
    mode against production.

    Backends:

    * SQLite is detected via ``hasattr(graph_store, "_conn")``.
    * Neo4j (sync) is detected via ``hasattr(graph_store, "_client")``.
    """
    if dry_run == apply:
        raise ValueError("must pass exactly one of dry_run=True or apply=True")
    if not isinstance(owner_sub, str) or not owner_sub.strip():
        raise ValueError("owner_sub must be a non-empty identity string")
    owner_sub = owner_sub.strip()

    if hasattr(graph_store, "_conn"):
        return _migrate_tenancy_sqlite(graph_store, owner_sub, dry_run=dry_run, apply=apply)
    if hasattr(graph_store, "_client"):
        return _migrate_tenancy_neo4j(graph_store, owner_sub, dry_run=dry_run, apply=apply)
    raise TypeError(
        "migrate_tenancy requires a SqliteGraphStore or Neo4jGraphStore; "
        f"got {type(graph_store).__name__}"
    )


_SQL_NODES_NEED_STAMP = (
    "(json_extract(props, '$.org_id') IS NULL  OR json_extract(props, '$.user_id') IS NULL)"
)
_SQL_NODES_ARE_ORPHAN = _SQL_NODES_NEED_STAMP + " AND (key_value IS NULL OR TRIM(key_value) = '')"


def _migrate_tenancy_sqlite(
    store: Any,
    owner_sub: str,
    *,
    dry_run: bool,
    apply: bool,
) -> dict[str, Any]:
    conn = store._conn
    # --- count + sample ------------------------------------------------
    node_total = conn.execute(
        f"SELECT COUNT(*) AS n FROM nodes WHERE {_SQL_NODES_NEED_STAMP}"
    ).fetchone()["n"]
    orphan_total = conn.execute(
        f"SELECT COUNT(*) AS n FROM nodes WHERE {_SQL_NODES_ARE_ORPHAN}"
    ).fetchone()["n"]
    rel_total = conn.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE org_id IS NULL OR user_id IS NULL"
    ).fetchone()["n"]
    node_sample = [
        {"label": r["label"], "key_value": r["key_value"]}
        for r in conn.execute(
            f"SELECT label, key_value FROM nodes WHERE {_SQL_NODES_NEED_STAMP} LIMIT 10"
        ).fetchall()
    ]

    report: dict[str, Any] = {
        "owner_sub": owner_sub,
        "dry_run": dry_run,
        # The "real" stamp work excludes orphans (those get purged below).
        "nodes_to_stamp": max(0, node_total - orphan_total),
        "relations_to_stamp": rel_total,
        "orphans_to_purge": orphan_total,
        "sample": {"nodes_to_stamp": node_sample},
    }
    if dry_run:
        return report

    # --- apply ---------------------------------------------------------
    # 1) Stamp identity onto every node that lacks it (and isn't an orphan).
    nodes_stamped = conn.execute(
        "UPDATE nodes SET "
        "  props = json_set(props, '$.org_id', ?, '$.user_id', ?) "
        f"WHERE {_SQL_NODES_NEED_STAMP} "
        "  AND NOT (key_value IS NULL OR TRIM(key_value) = '')",
        (owner_sub, owner_sub),
    ).rowcount
    # 2) Stamp identity onto every edge that lacks it. Edges' endpoint
    #    nodes were stamped just above, so by the time this UPDATE runs,
    #    every edge whose org_id/user_id was NULL belongs to the same
    #    owner_sub.
    relations_stamped = conn.execute(
        "UPDATE edges SET org_id = ?, user_id = ? WHERE org_id IS NULL OR user_id IS NULL",
        (owner_sub, owner_sub),
    ).rowcount
    # 3) Purge true orphans (no identity AND no merge key). Any edges that
    #    referenced them go away via ON DELETE CASCADE.
    orphans_purged = conn.execute(f"DELETE FROM nodes WHERE {_SQL_NODES_ARE_ORPHAN}").rowcount
    conn.commit()

    report.update(
        {
            "nodes_stamped": nodes_stamped,
            "relations_stamped": relations_stamped,
            "orphans_purged": orphans_purged,
        }
    )
    return report


# ---------------------------------------------------------------------------
# Spec 001 US-3 / T028-T029 — GDPR right-to-erasure
# ---------------------------------------------------------------------------


def gdpr_forget(
    graph_store: Any,
    vector_store: Any,
    *,
    org_id: str,
    user_id: str,
    dry_run: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Physically erase every node, relation and embedding of one identity.

    GDPR right-to-erasure (Spec 001, US-3). Unlike the fail-closed read
    filter — which only *hides* other tenants' data — this destroys the
    target identity's rows outright: there is no soft-delete and no
    server-side backup (a retained copy would defeat the erasure and
    create a fresh PII liability; the user downloads their own copy via
    the export tool *before* calling this).

    Contract:

    * ``dry_run=True`` counts what *would* be deleted and writes nothing.
    * ``apply=True`` deletes the rows. Idempotent — a second apply on an
      already-erased identity reports all zeros.
    * Exactly one of ``dry_run`` / ``apply`` MUST be set.
    * Only the ``(org_id, user_id)`` identity is touched; every other
      tenant is left byte-for-byte intact.

    Returns a Deletion report (Spec 001 data-model.md)::

        {"org_id", "user_id", "deleted_nodes_by_label": {label: n},
         "deleted_relations": n, "deleted_embeddings": n,
         "deleted_notes": n, "timestamp": iso8601}

    Backends:

    * SQLite — ``hasattr(graph_store, "_conn")``. Deletes ``nodes`` +
      ``edges`` by scope and the vec0 ``node_embeddings`` rows for those
      node ids (vec0 has no FK cascade, so embeddings are deleted first).
    * Neo4j (sync) — ``hasattr(graph_store, "_client")``. ``DETACH DELETE``
      removes the node, its relationships and its ``embedding`` property
      (and the vector-index entry) in one step.
    """
    if dry_run == apply:
        raise ValueError("must pass exactly one of dry_run=True or apply=True")
    if not isinstance(org_id, str) or not org_id.strip():
        raise ValueError("org_id must be a non-empty identity string")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id must be a non-empty identity string")
    org_id = org_id.strip()
    user_id = user_id.strip()

    if hasattr(graph_store, "_conn"):
        return _gdpr_forget_sqlite(
            graph_store, vector_store, org_id=org_id, user_id=user_id, dry_run=dry_run, apply=apply
        )
    if hasattr(graph_store, "_client"):
        return _gdpr_forget_neo4j(
            graph_store, vector_store, org_id=org_id, user_id=user_id, dry_run=dry_run, apply=apply
        )
    raise TypeError(
        "gdpr_forget requires a SqliteGraphStore or Neo4jGraphStore; "
        f"got {type(graph_store).__name__}"
    )


# Node identity lives in the JSON ``props`` blob; edges carry it as columns.
_SQL_SCOPE_NODES = "json_extract(props, '$.org_id') = ? AND json_extract(props, '$.user_id') = ?"


def _count_embeddings_sqlite(conn: Any, vector_store: Any, node_ids: list[int]) -> int:
    """Count vec0 rows for ``node_ids``, tolerating an absent index table
    (dimensionless install / embeddings never created)."""
    if not node_ids:
        return 0
    index_name = getattr(vector_store, "_index_name", "node_embeddings")
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (index_name,)).fetchone()
    if not exists:
        return 0
    placeholders = ",".join("?" * len(node_ids))
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM {index_name} WHERE node_id IN ({placeholders})",
        node_ids,
    ).fetchone()["n"]


def _gdpr_forget_sqlite(
    store: Any,
    vector_store: Any,
    *,
    org_id: str,
    user_id: str,
    dry_run: bool,
    apply: bool,
) -> dict[str, Any]:
    conn = store._conn
    scope = (org_id, user_id)

    node_rows = conn.execute(
        f"SELECT id, label FROM nodes WHERE {_SQL_SCOPE_NODES}", scope
    ).fetchall()
    node_ids = [r["id"] for r in node_rows]
    by_label: dict[str, int] = {}
    for r in node_rows:
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
    rel_total = conn.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE org_id = ? AND user_id = ?", scope
    ).fetchone()["n"]
    embed_total = _count_embeddings_sqlite(conn, vector_store, node_ids)

    report: dict[str, Any] = {
        "org_id": org_id,
        "user_id": user_id,
        "deleted_nodes_by_label": by_label,
        "deleted_relations": rel_total,
        "deleted_embeddings": embed_total,
        "deleted_notes": 0,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if dry_run:
        return report

    # Order: embeddings first (vec0 has no FK cascade), then edges, then
    # nodes. Deleting nodes would cascade their edges, but we delete edges
    # by scope explicitly so an org-shared edge of this identity goes too.
    if node_ids:
        vector_store.delete_vectors([str(n) for n in node_ids])
        # ``nodes_fts`` is a content-storing FTS5 table (not external-content),
        # so the erased subject's indexed PII (name/title/description/notes/…)
        # survives a ``DELETE FROM nodes`` unless we remove its rows too. Every
        # other hard-delete path does this; GDPR erasure must as well, or the
        # data is not actually forgotten (Art. 17). See backends/sqlite/store.py.
        placeholders = ",".join("?" * len(node_ids))
        conn.execute(
            f"DELETE FROM nodes_fts WHERE rowid IN ({placeholders})",
            [int(n) for n in node_ids],
        )
    conn.execute("DELETE FROM edges WHERE org_id = ? AND user_id = ?", scope)
    conn.execute(f"DELETE FROM nodes WHERE {_SQL_SCOPE_NODES}", scope)
    conn.commit()
    return report


def _gdpr_forget_neo4j(
    store: Any,
    vector_store: Any,
    *,
    org_id: str,
    user_id: str,
    dry_run: bool,
    apply: bool,
) -> dict[str, Any]:
    client = store._client
    params = {"org": org_id, "user": user_id}

    label_rows = client.run(
        "MATCH (n {org_id: $org, user_id: $user}) RETURN labels(n)[0] AS label, count(n) AS n",
        params,
    )
    by_label = {r["label"]: r["n"] for r in label_rows if r["n"]}
    rel_total = client.run(
        "MATCH ()-[r {org_id: $org, user_id: $user}]->() RETURN count(r) AS n", params
    )[0]["n"]
    embed_total = client.run(
        "MATCH (n {org_id: $org, user_id: $user}) WHERE n.embedding IS NOT NULL "
        "RETURN count(n) AS n",
        params,
    )[0]["n"]

    report: dict[str, Any] = {
        "org_id": org_id,
        "user_id": user_id,
        "deleted_nodes_by_label": by_label,
        "deleted_relations": rel_total,
        "deleted_embeddings": embed_total,
        "deleted_notes": 0,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if dry_run:
        return report

    # DETACH DELETE drops the node, its relationships and its embedding
    # property (and the vector-index entry) atomically.
    client.run("MATCH (n {org_id: $org, user_id: $user}) DETACH DELETE n", params)
    return report


_CYPHER_NODES_NEED_STAMP = "MATCH (n) WHERE n.org_id IS NULL OR n.user_id IS NULL"


def _migrate_tenancy_neo4j(
    store: Any,
    owner_sub: str,
    *,
    dry_run: bool,
    apply: bool,
) -> dict[str, Any]:
    client = store._client
    node_total = client.run(f"{_CYPHER_NODES_NEED_STAMP} RETURN count(n) AS n")[0]["n"]
    rel_total = client.run(
        "MATCH ()-[r]->() WHERE r.org_id IS NULL OR r.user_id IS NULL RETURN count(r) AS n"
    )[0]["n"]
    # Orphan: identity-less AND missing both possible merge keys.
    orphan_total = client.run(
        "MATCH (n) WHERE (n.org_id IS NULL OR n.user_id IS NULL) "
        "AND n.name IS NULL AND n.title IS NULL "
        "RETURN count(n) AS n"
    )[0]["n"]
    node_sample = [
        {"label": r["label"], "key_value": r["key"]}
        for r in client.run(
            "MATCH (n) WHERE n.org_id IS NULL OR n.user_id IS NULL "
            "RETURN labels(n)[0] AS label, "
            "coalesce(n.name, n.title) AS key LIMIT 10"
        )
    ]

    report: dict[str, Any] = {
        "owner_sub": owner_sub,
        "dry_run": dry_run,
        "nodes_to_stamp": max(0, node_total - orphan_total),
        "relations_to_stamp": rel_total,
        "orphans_to_purge": orphan_total,
        "sample": {"nodes_to_stamp": node_sample},
    }
    if dry_run:
        return report

    nodes_stamped = client.run(
        "MATCH (n) WHERE (n.org_id IS NULL OR n.user_id IS NULL) "
        "AND (n.name IS NOT NULL OR n.title IS NOT NULL) "
        "SET n.org_id = coalesce(n.org_id, $owner), "
        "    n.user_id = coalesce(n.user_id, $owner) "
        "RETURN count(n) AS n",
        {"owner": owner_sub},
    )[0]["n"]
    relations_stamped = client.run(
        "MATCH ()-[r]->() WHERE r.org_id IS NULL OR r.user_id IS NULL "
        "SET r.org_id = coalesce(r.org_id, $owner), "
        "    r.user_id = coalesce(r.user_id, $owner) "
        "RETURN count(r) AS n",
        {"owner": owner_sub},
    )[0]["n"]
    orphans_purged = client.run(
        "MATCH (n) WHERE (n.org_id IS NULL OR n.user_id IS NULL) "
        "AND n.name IS NULL AND n.title IS NULL "
        "DETACH DELETE n RETURN count(*) AS n"
    )[0]["n"]

    report.update(
        {
            "nodes_stamped": nodes_stamped,
            "relations_stamped": relations_stamped,
            "orphans_purged": orphans_purged,
        }
    )
    return report
