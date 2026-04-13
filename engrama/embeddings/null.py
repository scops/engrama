"""
Engrama — Null embedding provider.

Returns zero-dimension embeddings.  Used when ``EMBEDDING_PROVIDER=none``
(the default in Phase A).  Hybrid search gracefully degrades to
fulltext-only when the provider returns empty embeddings.
"""

from __future__ import annotations


class NullProvider:
    """An embedding provider that produces no embeddings.

    ``dimensions`` is 0 and all embed calls return empty lists.
    This signals to the search engine that vector search should be
    skipped (α forced to 0.0, fulltext only).
    """

    dimensions: int = 0

    def embed(self, text: str) -> list[float]:
        """Return an empty embedding."""
        return []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return empty embeddings for each text."""
        return [[] for _ in texts]

    def health_check(self) -> bool:
        """Null provider is always healthy."""
        return True

    def __repr__(self) -> str:
        return "NullProvider(dimensions=0)"

    # ------------------------------------------------------------------
    # Async API (for MCP server — mirrors sync methods)
    # ------------------------------------------------------------------

    async def aembed(self, text: str) -> list[float]:
        """Async no-op embed."""
        return []

    async def aembed_batch(self, texts: list[str]) -> list[list[float]]:
        """Async no-op batch embed."""
        return [[] for _ in texts]

    async def ahealth_check(self) -> bool:
        """Async no-op health check."""
        return True

    async def aclose(self) -> None:
        """No-op close."""
        pass
