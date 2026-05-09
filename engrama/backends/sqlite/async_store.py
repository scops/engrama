"""
Engrama — Async wrapper around the SQLite graph + vector store.

``SqliteAsyncStore`` is a thin façade that composes a sync
:class:`SqliteGraphStore` with a sync :class:`SqliteVecStore` and
exposes every method as a coroutine via :func:`asyncio.to_thread`.

We deliberately avoid duplicating the 30+ method bodies in async form.
``aiosqlite`` itself is just a thread-pool over ``sqlite3``; doing the
same wrapping here keeps the sync code as the single source of truth.
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any

from engrama.backends.sqlite.store import SqliteGraphStore
from engrama.backends.sqlite.vector import SqliteVecStore


# Methods handled by the vector store. They take precedence over the
# graph store when names collide (none currently collide, but listing
# them explicitly makes the routing intentional).
_VECTOR_METHODS = frozenset({
    "store_vectors", "store_vector_by_key", "delete_vectors",
    "search_vectors", "search_similar", "count", "count_embeddings",
    "ensure_index",
})


class SqliteAsyncStore:
    """Async ``GraphStore`` + ``VectorStore`` over SQLite.

    Parameters:
        path: Database path (or ``":memory:"``).
        vector_dimensions: Embedding dim. ``0`` disables vector ops.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        vector_dimensions: int = 0,
    ) -> None:
        self._sync = SqliteGraphStore(path)
        self._vector = SqliteVecStore(self._sync._conn, vector_dimensions)
        if vector_dimensions:
            self._vector.ensure_index()

    # ------------------------------------------------------------------
    # Sync attributes that callers expect at construction time.
    # ------------------------------------------------------------------

    @property
    def dimensions(self) -> int:
        return self._vector.dimensions

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await asyncio.to_thread(self._sync.close)

    # ------------------------------------------------------------------
    # Auto-async delegation
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Forward any other method call to sync graph or vector store,
        wrapping it in ``asyncio.to_thread`` so the caller awaits a
        coroutine.

        ``__getattr__`` is only invoked when the attribute is NOT found
        on ``self`` (so ``dimensions``, ``close``, etc. defined above
        keep their explicit behaviour).
        """
        target_attr: Any = None
        if name in _VECTOR_METHODS:
            target_attr = getattr(self._vector, name, None)
        if target_attr is None:
            target_attr = getattr(self._sync, name, None)
        if target_attr is None:
            target_attr = getattr(self._vector, name, None)
        if target_attr is None:
            raise AttributeError(
                f"{type(self).__name__!r} has no attribute {name!r}"
            )
        if not callable(target_attr):
            return target_attr

        @functools.wraps(target_attr)
        async def _async_call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(target_attr, *args, **kwargs)

        return _async_call
