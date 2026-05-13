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
