"""
Engrama — Memory engine.

:class:`EngramaEngine` is the main write/read pipeline for the memory graph.
It delegates all storage operations to a ``GraphStore`` backend (see
:mod:`engrama.core.protocols`), enforcing the project's invariants:

* Every write uses ``MERGE`` — never bare ``CREATE``.
* Every node receives ``created_at`` (set once) and ``updated_at`` (refreshed).
* All Cypher uses ``$param`` parameters — no string formatting.

**Backward compatibility:** The constructor still accepts an
:class:`~engrama.core.client.EngramaClient` and wraps it in a
:class:`~engrama.backends.neo4j.backend.Neo4jGraphStore` automatically.
"""

from __future__ import annotations

import logging
from typing import Any

from engrama.core.client import EngramaClient
from engrama.core.schema import TITLE_KEYED_LABELS
from engrama.core.security import Provenance

logger = logging.getLogger("engrama.core.engine")


class EngramaEngine:
    """High-level read/write interface for the Engrama memory graph.

    Parameters:
        client_or_store: Either a legacy :class:`EngramaClient` (sync
            Neo4j driver wrapper) **or** a ``GraphStore`` implementation
            such as :class:`~engrama.backends.neo4j.backend.Neo4jGraphStore`
            or :class:`~engrama.backends.null.NullGraphStore`.
        vector_store: Optional ``VectorStore`` for embedding storage.
        embedder: Optional ``EmbeddingProvider`` for generating embeddings
            on write.  When both *vector_store* and *embedder* are provided
            (and the embedder has ``dimensions > 0``), new nodes are
            automatically embedded during :meth:`merge_node`.

    When an :class:`EngramaClient` is passed, it is automatically wrapped
    in a :class:`Neo4jGraphStore` so that all internal methods use the
    protocol-based backend.  Existing code that creates an engine as
    ``EngramaEngine(client)`` continues to work unchanged.
    """

    def __init__(
        self,
        client_or_store: Any,
        vector_store: Any = None,
        embedder: Any = None,
        *,
        default_provenance: Provenance | None = None,
    ) -> None:
        if isinstance(client_or_store, EngramaClient):
            from engrama.backends.neo4j.backend import Neo4jGraphStore

            self._store = Neo4jGraphStore(client_or_store)
            self._client = client_or_store
        else:
            self._store = client_or_store
            self._client = getattr(client_or_store, "client", None)

        self._vector_store = vector_store
        self._embedder = embedder
        self._embed_on_write: bool = (
            embedder is not None
            and vector_store is not None
            and getattr(embedder, "dimensions", 0) > 0
            and getattr(vector_store, "dimensions", 0) > 0
        )
        self.default_provenance: Provenance | None = default_provenance

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def merge_node(
        self,
        label: str,
        properties: dict[str, Any],
        *,
        provenance: Provenance | None = None,
    ) -> list[dict[str, Any]]:
        """Create or update a node using ``MERGE``.

        DDR-003 Phase D: temporal fields ``valid_from``, ``valid_to``,
        and ``confidence`` flow through to the backend.  Conflict detection
        (reviving expired nodes) is handled in the backend's ON MATCH clause.

        DDR-003 Phase E: provenance flows through as four flat properties
        (``source``, ``source_agent``, ``source_session``, ``trust_level``).
        Explicit ``provenance`` wins over the engine's ``default_provenance``;
        if neither is set, no provenance is recorded.
        """
        if "name" in properties:
            merge_key = "name"
        elif "title" in properties:
            merge_key = "title"
        else:
            raise ValueError("properties must include 'name' or 'title' as a merge key")

        merge_value = properties[merge_key]

        effective_provenance = provenance or self.default_provenance
        if effective_provenance is not None:
            prov_props = effective_provenance.to_properties()
            properties = {**prov_props, **properties}

        extra_props = {
            k: v for k, v in properties.items() if k not in {merge_key, "created_at", "updated_at"}
        }

        # --- Embed on write (DDR-003 Phase C) ---
        embedding: list[float] | None = None
        if self._embed_on_write:
            try:
                from engrama.embeddings.text import node_to_text

                text = node_to_text(label, properties)
                embedding = self._embedder.embed(text)
            except Exception as e:
                logger.warning("Embedding failed for %s/%s: %s", label, merge_value, e)
                embedding = None

        # --- Guard against degenerate embeddings (issue #18) ---
        # A None / empty / zero-norm vector cannot serve as a similarity
        # key — storing it pollutes the hybrid-search ranking because
        # cosine similarity is undefined (or uniformly maximal) against
        # such a vector. Drop the embedding and flag the node so the
        # next ``engrama reindex`` run picks it up. If the embedding is
        # healthy we clear any pre-existing flag (self-healing on
        # re-merge — exactly the workaround the issue documented).
        from engrama.embeddings.health import is_degenerate_vector

        if self._embed_on_write:
            if is_degenerate_vector(embedding):
                if embedding is not None:  # None already warned above
                    logger.warning(
                        "Embedding for %s/%s came back degenerate "
                        "(len=%d, near-zero norm); skipping vector "
                        "storage and flagging needs_reindex=true",
                        label,
                        merge_value,
                        len(embedding),
                    )
                extra_props["needs_reindex"] = True
                embedding = None
            else:
                extra_props["needs_reindex"] = False

        result = self._store.merge_node(
            label,
            merge_key,
            merge_value,
            extra_props,
            embedding=embedding,
        )

        # Store vector via VectorStore (adds :Embedded label for index)
        if embedding and self._vector_store is not None:
            try:
                store_by_key = getattr(self._vector_store, "store_vector_by_key", None)
                if store_by_key:
                    store_by_key(label, merge_key, merge_value, embedding)
            except Exception as e:
                logger.warning("Vector store failed for %s/%s: %s", label, merge_value, e)

        return result

    def merge_relation(
        self,
        from_name: str,
        from_label: str,
        rel_type: str,
        to_name: str,
        to_label: str,
    ) -> list[dict[str, Any]]:
        """Create or update a relationship between two existing nodes."""
        from_key = "title" if from_label in TITLE_KEYED_LABELS else "name"
        to_key = "title" if to_label in TITLE_KEYED_LABELS else "name"

        return self._store.merge_relation(
            from_label,
            from_key,
            from_name,
            rel_type,
            to_label,
            to_key,
            to_name,
        )

    def run(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a raw Cypher query (delegates to the backend)."""
        return self._store.run_cypher(query, params)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Run a fulltext search against the ``memory_search`` index."""
        return self._store.fulltext_search(query, limit=limit)

    def hybrid_search(self, query: str, limit: int = 10) -> list[Any]:
        """Run a hybrid search combining fulltext and vector similarity.

        Falls back to plain fulltext search when no embedder/vector store
        is configured.
        """
        if self._embed_on_write:
            from engrama.core.search import HybridSearchEngine

            engine = HybridSearchEngine(
                self._store,
                self._vector_store,
                self._embedder,
            )
            return engine.search(query, limit=limit)

        # Fallback: plain fulltext, wrapped as SearchResult for consistency
        from engrama.core.search import SearchResult

        records = self._store.fulltext_search(query, limit=limit)
        results = []
        for r in records:
            d = dict(r) if not isinstance(r, dict) else r
            results.append(
                SearchResult(
                    label=d.get("type", ""),
                    name=d.get("name", ""),
                    fulltext_score=d.get("score", 0.0),
                    final_score=d.get("score", 0.0),
                )
            )
        return results

    def decay_scores(
        self,
        rate: float = 0.01,
        min_confidence: float = 0.0,
        max_age_days: int = 0,
        label: str | None = None,
    ) -> dict[str, int]:
        """Batch-apply confidence decay (delegates to the backend).

        Returns:
            Dict with ``decayed`` and ``archived`` counts.
        """
        fn = getattr(self._store, "decay_scores", None)
        if fn is None:
            logger.warning("Backend does not support decay_scores")
            return {"decayed": 0, "archived": 0}
        return fn(
            rate=rate,
            min_confidence=min_confidence,
            max_age_days=max_age_days,
            label=label,
        )

    def get_context(self, name: str, label: str, hops: int = 1) -> list[dict[str, Any]]:
        """Retrieve the local neighbourhood of a node."""
        return self._store.get_neighbours(label, "name", name, hops=hops)
