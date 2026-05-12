"""
Engrama — Backend factory.

Reads configuration from environment variables (or a ``dict``) and
returns the appropriate ``GraphStore`` + ``VectorStore`` implementations.

Default backend is ``sqlite`` (zero dependency, file-based) so a
base install works without Docker, JVM, or any external service
(``uv sync`` from a source checkout; ``pip install engrama`` once
the package ships on PyPI). ``GRAPH_BACKEND=neo4j`` opts in to the
production backend (install with ``uv sync --extra neo4j`` /
``pip install engrama[neo4j]``).

Usage::

    from engrama.backends import create_stores, create_async_stores

    graph, vector = create_stores()              # sync, reads .env
    graph, vector = await create_async_stores()  # async (MCP server)
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()


_DEFAULT_GRAPH_BACKEND = "sqlite"


def _neo4j_extra_error_message() -> str:
    """Return a consistent install hint for Neo4j-backed execution."""
    return (
        "GRAPH_BACKEND=neo4j requires the optional 'neo4j' Python dependency. "
        "Install it with `uv sync --extra neo4j` "
        "(or `pip install engrama[neo4j]` once Engrama ships on PyPI)."
    )


def _resolve(config: dict[str, Any], key: str, default: str | None = None) -> str | None:
    """Look up a value in ``config`` first, then env var, then default."""
    value = config.get(key) if config else None
    if value is None:
        value = os.getenv(key)
    if value is None:
        return default
    return value


def _default_vector_for(graph_backend: str) -> str:
    """When the user hasn't picked a vector backend, infer from graph
    backend so each combo has a sensible zero-config default.
    """
    if graph_backend == "sqlite":
        return "sqlite-vec"
    if graph_backend == "neo4j":
        # Neo4j vector index requires extra setup; opt-in only.
        return "none"
    return "none"


# ---------------------------------------------------------------------------
# Sync factory
# ---------------------------------------------------------------------------


def create_stores(
    config: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """Create graph and vector store instances from configuration.

    Parameters:
        config: Optional config dict. Falls back to env vars. Recognised
            keys: ``GRAPH_BACKEND``, ``VECTOR_BACKEND``,
            ``ENGRAMA_DB_PATH`` (sqlite), ``NEO4J_*`` (neo4j),
            ``EMBEDDING_DIMENSIONS``.

    Returns:
        ``(graph_store, vector_store)``. For SQLite both are typically
        the same underlying connection.
    """
    cfg = config or {}
    graph_backend = _resolve(cfg, "GRAPH_BACKEND", _DEFAULT_GRAPH_BACKEND) or _DEFAULT_GRAPH_BACKEND
    vector_backend = _resolve(cfg, "VECTOR_BACKEND", _default_vector_for(graph_backend))

    graph_store = _create_graph_store(graph_backend, cfg)
    vector_store = _create_vector_store(vector_backend, cfg, graph_store)
    return graph_store, vector_store


def _create_graph_store(backend: str, config: dict[str, Any]) -> Any:
    if backend == "sqlite":
        from engrama.backends.sqlite import SqliteGraphStore

        path = _resolve(config, "ENGRAMA_DB_PATH", _default_db_path())
        return SqliteGraphStore(path)

    if backend == "neo4j":
        try:
            from engrama.backends.neo4j.backend import Neo4jGraphStore
            from engrama.core.client import EngramaClient
        except ImportError as e:
            raise ImportError(_neo4j_extra_error_message()) from e

        client = EngramaClient(
            uri=_resolve(config, "NEO4J_URI"),
            user=_resolve(config, "NEO4J_USERNAME"),
            password=_resolve(config, "NEO4J_PASSWORD"),
        )
        return Neo4jGraphStore(client)

    if backend == "null":
        from engrama.backends.null import NullGraphStore

        return NullGraphStore()

    raise ValueError(f"Unknown graph backend: {backend!r}. Supported: 'sqlite', 'neo4j', 'null'.")


def _create_vector_store(
    backend: str,
    config: dict[str, Any],
    graph_store: Any,
) -> Any:
    if backend == "sqlite-vec":
        from engrama.backends.sqlite import SqliteVecStore

        # Reuse the connection from the SqliteGraphStore so vectors live
        # in the same database file as the nodes/edges.
        conn = getattr(graph_store, "_conn", None)
        if conn is None:
            raise ValueError(
                "VECTOR_BACKEND=sqlite-vec requires a SqliteGraphStore (set GRAPH_BACKEND=sqlite)."
            )
        dims = int(_resolve(config, "EMBEDDING_DIMENSIONS", "0") or 0)
        store = SqliteVecStore(conn, dimensions=dims)
        if dims:
            store.ensure_index()
        return store

    if backend == "neo4j":
        from engrama.backends.neo4j.vector import Neo4jVectorStore

        client = getattr(graph_store, "_client", None)
        if client is None:
            raise ValueError("VECTOR_BACKEND=neo4j requires GRAPH_BACKEND=neo4j (shared client).")
        dims = int(_resolve(config, "EMBEDDING_DIMENSIONS", "768") or 768)
        store = Neo4jVectorStore(client, dimensions=dims)
        store.ensure_index()
        return store

    if backend in ("none", "null"):
        from engrama.backends.null import NullVectorStore

        return NullVectorStore()

    raise ValueError(
        f"Unknown vector backend: {backend!r}. Supported: 'sqlite-vec', 'neo4j', 'none'."
    )


# ---------------------------------------------------------------------------
# Async factory
# ---------------------------------------------------------------------------


def create_async_stores(
    config: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """Async equivalent of :func:`create_stores`.

    Returns ``(graph_store, vector_store)`` where each store exposes
    coroutine methods. SQLite uses ``asyncio.to_thread`` under the
    hood; Neo4j uses its native ``AsyncDriver``.

    The store owns any underlying driver/connection it created — call
    ``await graph_store.close()`` on shutdown.
    """
    cfg = config or {}
    graph_backend = _resolve(cfg, "GRAPH_BACKEND", _DEFAULT_GRAPH_BACKEND) or _DEFAULT_GRAPH_BACKEND
    vector_backend = _resolve(cfg, "VECTOR_BACKEND", _default_vector_for(graph_backend))

    if graph_backend == "sqlite":
        from engrama.backends.sqlite import SqliteAsyncStore

        path = _resolve(cfg, "ENGRAMA_DB_PATH", _default_db_path())
        # SqliteAsyncStore composes a SqliteVecStore internally, so the
        # same instance satisfies both protocols.
        dims = int(_resolve(cfg, "EMBEDDING_DIMENSIONS", "0") or 0)
        # Vector backend choice is mostly cosmetic for SQLite — the
        # composed vector store either uses sqlite-vec (when dims>0)
        # or no-ops. We honour an explicit "none" by passing dims=0.
        if vector_backend in ("none", "null"):
            dims = 0
        store = SqliteAsyncStore(path, vector_dimensions=dims)
        return store, store

    if graph_backend == "neo4j":
        try:
            from neo4j import AsyncGraphDatabase

            from engrama.backends.neo4j.async_store import Neo4jAsyncStore
        except ImportError as e:
            raise ImportError(_neo4j_extra_error_message()) from e

        uri = _resolve(cfg, "NEO4J_URI", "bolt://localhost:7687")
        user = _resolve(cfg, "NEO4J_USERNAME", "neo4j")
        password = _resolve(cfg, "NEO4J_PASSWORD")
        database = _resolve(cfg, "NEO4J_DATABASE", "neo4j") or "neo4j"
        if not password:
            raise ValueError(
                "GRAPH_BACKEND=neo4j requires NEO4J_PASSWORD (env var, .env, or explicit config)."
            )
        driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        dims = int(_resolve(cfg, "EMBEDDING_DIMENSIONS", "0") or 0)
        store = Neo4jAsyncStore(
            driver,
            database=database,
            vector_dimensions=dims,
        )
        # Mark the store as owner so its close() shuts the driver down.
        store._owns_driver = True  # type: ignore[attr-defined]
        return store, store

    if graph_backend == "null":
        from engrama.backends.null import NullGraphStore, NullVectorStore

        # Null stores are sync-only; wrap if we ever need async there.
        return NullGraphStore(), NullVectorStore()

    raise ValueError(
        f"Unknown async graph backend: {graph_backend!r}. Supported: 'sqlite', 'neo4j', 'null'."
    )


# ---------------------------------------------------------------------------
# Embedding provider (delegates to engrama.embeddings)
# ---------------------------------------------------------------------------


def create_embedding_provider(
    config: dict[str, Any] | None = None,
) -> Any:
    """Create an embedding provider from configuration.

    Delegates to :func:`engrama.embeddings.create_provider`. Returns a
    provider whose ``dimensions`` may be ``0`` (NullProvider) when no
    embedding backend is configured.
    """
    from engrama.embeddings import create_provider

    return create_provider(config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_db_path() -> str:
    """Default SQLite database path under the user's home (~/.engrama/engrama.db)."""
    return str(os.path.expanduser("~/.engrama/engrama.db"))
