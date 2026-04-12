"""
Engrama — Backend factory.

Reads configuration from environment variables (or a ``dict``) and returns
the appropriate ``GraphStore`` + ``VectorStore`` implementations.

Usage::

    from engrama.backends import create_stores

    graph, vector = create_stores()       # reads .env
    graph, vector = create_stores(config)  # explicit config dict
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def create_stores(
    config: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """Create graph and vector store instances from configuration.

    Parameters:
        config: Optional config dict.  If ``None``, reads from env vars.
                Expected keys: ``GRAPH_BACKEND``, ``VECTOR_BACKEND``,
                ``NEO4J_URI``, ``NEO4J_USERNAME``, ``NEO4J_PASSWORD``,
                ``NEO4J_DATABASE``.

    Returns:
        A ``(graph_store, vector_store)`` tuple.
    """
    if config is None:
        config = {}

    graph_backend = config.get("GRAPH_BACKEND") or os.getenv("GRAPH_BACKEND", "neo4j")
    vector_backend = config.get("VECTOR_BACKEND") or os.getenv("VECTOR_BACKEND", "none")

    graph_store = _create_graph_store(graph_backend, config)
    vector_store = _create_vector_store(vector_backend, config, graph_store)

    return graph_store, vector_store


def _create_graph_store(backend: str, config: dict[str, Any]) -> Any:
    """Instantiate the graph store for the given backend name."""
    if backend == "neo4j":
        from engrama.backends.neo4j.backend import Neo4jGraphStore
        from engrama.core.client import EngramaClient

        uri = config.get("NEO4J_URI") or os.getenv("NEO4J_URI")
        user = config.get("NEO4J_USERNAME") or os.getenv("NEO4J_USERNAME")
        password = config.get("NEO4J_PASSWORD") or os.getenv("NEO4J_PASSWORD")

        client = EngramaClient(uri=uri, user=user, password=password)
        return Neo4jGraphStore(client)

    if backend == "null":
        from engrama.backends.null import NullGraphStore
        return NullGraphStore()

    raise ValueError(
        f"Unknown graph backend: {backend!r}. "
        f"Supported: 'neo4j', 'null'."
    )


def _create_vector_store(
    backend: str,
    config: dict[str, Any],
    graph_store: Any,
) -> Any:
    """Instantiate the vector store for the given backend name."""
    if backend == "neo4j":
        from engrama.backends.neo4j.vector import Neo4jVectorStore

        # Reuse the same EngramaClient from the graph store
        client = getattr(graph_store, "_client", None)
        if client is None:
            raise ValueError(
                "VECTOR_BACKEND=neo4j requires GRAPH_BACKEND=neo4j "
                "(shared client)."
            )

        dimensions = int(
            config.get("EMBEDDING_DIMENSIONS")
            or os.getenv("EMBEDDING_DIMENSIONS", "768")
        )
        store = Neo4jVectorStore(client, dimensions=dimensions)
        # Ensure the vector index exists (idempotent)
        store.ensure_index()
        return store

    if backend in ("none", "null"):
        from engrama.backends.null import NullVectorStore
        return NullVectorStore()

    raise ValueError(
        f"Unknown vector backend: {backend!r}. "
        f"Supported: 'neo4j', 'none'."
    )


def create_embedding_provider(
    config: dict[str, Any] | None = None,
) -> Any:
    """Create an embedding provider from configuration.

    Delegates to :func:`engrama.embeddings.create_provider`.

    Parameters:
        config: Optional config dict.  If ``None``, reads from env vars.

    Returns:
        An ``EmbeddingProvider`` instance.
    """
    from engrama.embeddings import create_provider

    return create_provider(config)
