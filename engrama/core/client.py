"""
Engrama — Neo4j driver wrapper.

Provides :class:`EngramaClient`, a thin façade around the official
``neo4j`` Python driver that handles connection pooling, health checks,
and parameterised Cypher execution.  Credentials are resolved in order:

1. Explicit constructor arguments.
2. Environment variables (``NEO4J_URI``, ``NEO4J_USERNAME``, ``NEO4J_PASSWORD``).
3. Values loaded from a ``.env`` file via *python-dotenv*.
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase, Driver, Record


# Load .env so credentials are available even when the caller does not
# set environment variables explicitly.
load_dotenv()

# Defaults — password is intentionally omitted to force explicit config.
_DEFAULT_URI = "bolt://localhost:7687"
_DEFAULT_USER = "neo4j"


class EngramaClient:
    """Manages a Neo4j driver instance for the Engrama memory graph.

    Usage::

        client = EngramaClient()
        client.verify()
        records = client.run("MATCH (n:Project) RETURN n.name AS name LIMIT 5")
        client.close()

    Parameters:
        uri: Bolt URI for the Neo4j instance.  Falls back to
             ``NEO4J_URI`` env var, then ``bolt://localhost:7687``.
        user: Authentication user name.  Falls back to
              ``NEO4J_USERNAME`` env var, then ``"neo4j"``.
        password: Authentication password.  Falls back to
                  ``NEO4J_PASSWORD`` env var.  Raises if not set.
    """

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self._uri: str = uri or os.getenv("NEO4J_URI", _DEFAULT_URI)
        self._user: str = user or os.getenv("NEO4J_USERNAME", _DEFAULT_USER)
        resolved_password = password or os.getenv("NEO4J_PASSWORD")
        if not resolved_password:
            raise ValueError(
                "Neo4j password is required. Pass it explicitly, set NEO4J_PASSWORD "
                "in the environment, or create a .env file (see .env.example)."
            )
        self._password: str = resolved_password

        self._driver: Driver = GraphDatabase.driver(
            self._uri,
            auth=(self._user, self._password),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(self) -> None:
        """Check that Neo4j is reachable and credentials are valid.

        Raises:
            neo4j.exceptions.ServiceUnavailable: If the server cannot be
                reached at the configured URI.
            neo4j.exceptions.AuthError: If the credentials are rejected.
        """
        self._driver.verify_connectivity()

    def run(self, query: str, params: dict[str, Any] | None = None) -> list[Record]:
        """Execute a Cypher query and return the result records.

        All queries **must** use Cypher parameters (``$param`` syntax)
        rather than string formatting to prevent injection.

        Parameters:
            query: A parameterised Cypher query string.
            params: Optional mapping of parameter names to values.

        Returns:
            A list of :class:`neo4j.Record` objects.
        """
        with self._driver.session() as session:
            result = session.run(query, parameters=params or {})
            return list(result)

    def close(self) -> None:
        """Release the underlying driver and its connection pool."""
        self._driver.close()

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "EngramaClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"EngramaClient(uri={self._uri!r}, user={self._user!r})"
