"""
Engrama — Null (no-op) backends.

``NullGraphStore`` and ``NullVectorStore`` implement the storage protocols
with no persistence.  Useful for:

* Unit tests that don't need a database.
* Running Engrama in "dry-run" mode.
* Providing a safe default when no backend is configured.
"""

from __future__ import annotations

from typing import Any


class NullGraphStore:
    """A graph store that stores nothing and returns empty results.

    Implements the same method signatures as ``Neo4jGraphStore`` so it
    can be used as a drop-in replacement for testing.
    """

    def merge_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        properties: dict[str, Any],
        embedding: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        return []

    def get_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
    ) -> dict[str, Any] | None:
        return None

    def delete_node(
        self,
        label: str,
        key_field: str,
        key_value: str,
        soft: bool = True,
    ) -> bool:
        return False

    def merge_relation(
        self,
        from_label: str,
        from_key: str,
        from_value: str,
        rel_type: str,
        to_label: str,
        to_key: str,
        to_value: str,
    ) -> list[dict[str, Any]]:
        return []

    def get_neighbours(
        self,
        label: str,
        key_field: str,
        key_value: str,
        hops: int = 1,
        limit: int = 50,
        scope: Any = None,
    ) -> list[dict[str, Any]]:
        return []

    def fulltext_search(
        self,
        query: str,
        limit: int = 10,
        scope: Any = None,
    ) -> list[dict[str, Any]]:
        return []

    def run_cypher(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return []

    def init_schema(self, schema: Any = None) -> None:
        pass

    def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "backend": "null"}

    def close(self) -> None:
        pass

    @property
    def client(self) -> None:
        """Null store has no underlying client."""
        return None

    def __repr__(self) -> str:
        return "NullGraphStore()"


class NullVectorStore:
    """A vector store that stores nothing.

    Returns empty results for all queries.  Dimensions is 0.
    """

    dimensions: int = 0

    def store_vectors(
        self,
        items: list[tuple[str, list[float]]],
    ) -> int:
        return 0

    def search_vectors(
        self,
        query_embedding: list[float],
        limit: int = 10,
        scope: Any = None,
    ) -> list[dict[str, Any]]:
        return []

    def delete_vectors(
        self,
        node_ids: list[str],
    ) -> int:
        return 0

    def count(self) -> int:
        return 0

    def __repr__(self) -> str:
        return "NullVectorStore()"
