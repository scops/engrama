"""
Engrama — Storage and embedding protocols.

Defines the abstract interfaces that all backends must implement.
These are the contracts between the engine/skills layer and the
storage layer.  No skill or adapter should ever import a concrete
backend — only these protocols.

Phase A (DDR-003): Async protocols for future MCP/async usage.
The sync Neo4j backend mirrors these signatures synchronously.

.. note::
    ``GraphStore`` and ``VectorStore`` are separate protocols so that
    different backends can be mixed (e.g. Neo4j graph + ChromaDB vectors).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from engrama.core.scope import MemoryScope

# ---------------------------------------------------------------------------
# Schema definition (passed to GraphStore.init_schema)
# ---------------------------------------------------------------------------


@dataclass
class SchemaDefinition:
    """Describes the graph schema for a profile.

    Used by :meth:`GraphStore.init_schema` to create constraints,
    indexes, and seed data in a backend-agnostic way.
    """

    labels: list[str] = field(default_factory=list)
    """All valid node labels (e.g. ``["Project", "Concept", ...]``)."""

    relations: list[str] = field(default_factory=list)
    """All valid relationship types (e.g. ``["USES", "INFORMED_BY", ...]``)."""

    title_keyed_labels: frozenset[str] = field(default_factory=frozenset)
    """Labels that use ``title`` instead of ``name`` as merge key."""

    fulltext_properties: list[str] = field(
        default_factory=lambda: [
            "name",
            "title",
            "description",
            "notes",
            "rationale",
            "solution",
            "context",
            "body",
        ]
    )
    """Properties indexed by the fulltext search index."""


# ---------------------------------------------------------------------------
# GraphStore protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GraphStore(Protocol):
    """Abstract interface for graph storage backends.

    All methods are **async** for compatibility with MCP and other
    async callers.  Sync backends provide matching sync methods
    (same name, same signature, without ``async``).
    """

    # --- Node operations ---

    async def merge_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        properties: dict[str, Any],
        embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Create or update a node.  Always MERGE semantics."""
        ...

    async def get_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
    ) -> dict[str, Any] | None:
        """Retrieve a single node by its unique key."""
        ...

    async def delete_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        soft: bool = True,
    ) -> bool:
        """Delete or archive a node.  ``soft=True`` sets ``status='archived'``."""
        ...

    # --- Relationship operations ---

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
        """Create a relationship (idempotent)."""
        ...

    # --- Query operations ---

    async def get_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
        limit: int = 50,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Traverse N hops from a node, optionally filtered by scope.

        DDR-003 Phase F: when ``scope`` is set, only neighbours whose
        scope dimensions match (or are ``NULL``) are returned. Dimensions
        that are ``None`` on the supplied scope are not filtered.
        """
        ...

    async def fulltext_search(
        self,
        query: str,
        limit: int = 10,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """Keyword search across all text properties.

        DDR-003 Phase F: when ``scope`` is set, results are filtered as
        described on :meth:`get_neighbours`.

        Returns: ``[{node, score, label, key}]``
        """
        ...

    async def run_cypher(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a raw query (backend-specific).

        For reflect patterns that need full query power.
        Backends that don't support Cypher raise ``NotImplementedError``.
        """
        ...

    # --- Schema operations ---

    async def init_schema(self, schema: SchemaDefinition) -> None:
        """Apply constraints, indexes, and seed data."""
        ...

    async def health_check(self) -> dict[str, Any]:
        """Return backend status and version info."""
        ...

    # --- Lifecycle ---

    async def close(self) -> None:
        """Release resources (connections, pools)."""
        ...


# ---------------------------------------------------------------------------
# VectorStore protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class VectorStore(Protocol):
    """Abstract interface for vector similarity search.

    May be the same backend as ``GraphStore`` (e.g. Neo4j) or a
    separate one (e.g. ChromaDB, pgvector).
    """

    dimensions: int

    async def store_vectors(
        self,
        items: list[tuple[str, list[float]]],
    ) -> int:
        """Store embeddings for nodes.

        Args:
            items: ``[(node_id, embedding)]`` pairs.

        Returns:
            Count of vectors stored.
        """
        ...

    async def search_vectors(
        self,
        query_embedding: list[float],
        limit: int = 10,
        scope: MemoryScope | None = None,
    ) -> list[dict[str, Any]]:
        """k-ANN similarity search.

        Returns: ``[{node_id, score, label, key}]``
        """
        ...

    async def delete_vectors(
        self,
        node_ids: list[str],
    ) -> int:
        """Remove embeddings for deleted/archived nodes."""
        ...

    async def count(self) -> int:
        """Total vectors stored."""
        ...


# ---------------------------------------------------------------------------
# EmbeddingProvider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Abstract interface for embedding generation."""

    dimensions: int

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string."""
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of text strings."""
        ...

    async def health_check(self) -> bool:
        """Return ``True`` if the provider is available."""
        ...
