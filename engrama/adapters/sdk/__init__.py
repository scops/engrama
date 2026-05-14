"""
engrama/adapters/sdk/__init__.py

Clean public API for using Engrama as a Python library.

Usage::

    from engrama import Engrama

    with Engrama() as eng:
        # Remember something
        eng.remember("Technology", "FastAPI", "High-performance async web framework")

        # Recall context
        results = eng.recall("FastAPI", hops=2)
        for r in results:
            print(r.name, r.neighbours)

        # Associate two nodes
        eng.associate("MyProject", "Project", "USES", "FastAPI", "Technology")

        # Reflect — detect cross-entity patterns
        insights = eng.reflect()

        # Surface pending insights for review
        pending = eng.surface_insights()

        # Approve an insight and write to Obsidian
        eng.approve_insight("Some insight title")
        eng.write_insight_to_vault("Some insight title", "10-projects/my-project.md")

        # Forget — archive or purge
        eng.forget("Technology", "OldTech")
        eng.forget_by_ttl("Technology", days=365, purge=True)

        # Search
        hits = eng.search("microservices", limit=5)

Credentials are resolved from environment variables, ``.env`` file, or
explicit constructor arguments.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from engrama.core.engine import EngramaEngine
from engrama.core.security import Provenance
from engrama.skills.associate import AssociateSkill
from engrama.skills.forget import ForgetSkill
from engrama.skills.proactive import ProactiveSkill, SurfacedInsight
from engrama.skills.recall import RecallResult, RecallSkill
from engrama.skills.reflect import ReflectSkill
from engrama.skills.remember import RememberSkill


class Engrama:
    """High-level Python SDK for the Engrama memory graph.

    Wraps the engine, skills, and optional Obsidian adapter behind a
    single, convenient interface. Backend is selected by env (default
    ``GRAPH_BACKEND=sqlite``) or by passing explicit Neo4j credentials.

    Args:
        uri: Neo4j bolt URI. Passing this implicitly selects
            ``backend='neo4j'``. Falls back to ``NEO4J_URI`` env var.
        username: Neo4j user. Falls back to ``NEO4J_USERNAME`` env var.
        password: Neo4j password. Falls back to ``NEO4J_PASSWORD`` env var.
        backend: Explicit backend override (``"sqlite"`` or ``"neo4j"``).
            When ``None``, inferred from explicit Neo4j args, env, or
            falls back to the default (``sqlite``).
        db_path: SQLite database path (only used when backend is sqlite).
            Falls back to ``ENGRAMA_DB_PATH`` env var.
        vault_path: Obsidian vault path. Falls back to ``VAULT_PATH``
            env var. If ``None`` and no env var, Obsidian features are
            disabled.
        source_agent: Optional agent identifier persisted on every write
            as ``source_agent`` (DDR-003 Phase E provenance).
        source_session: Optional session identifier persisted on every
            write as ``source_session``.
    """

    def __init__(
        self,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        backend: str | None = None,
        db_path: str | Path | None = None,
        vault_path: str | Path | None = None,
        source_agent: str | None = None,
        source_session: str | None = None,
    ) -> None:
        from engrama.backends import create_embedding_provider, create_stores

        # Resolve backend: explicit > implied by Neo4j args > env > default.
        if backend is None:
            if any(v is not None for v in (uri, username, password)):
                backend = "neo4j"
        config: dict[str, Any] = {}
        if backend is not None:
            config["GRAPH_BACKEND"] = backend
        if uri is not None:
            config["NEO4J_URI"] = uri
        if username is not None:
            config["NEO4J_USERNAME"] = username
        if password is not None:
            config["NEO4J_PASSWORD"] = password
        if db_path is not None:
            config["ENGRAMA_DB_PATH"] = str(db_path)

        self._embedder = create_embedding_provider()
        # Push embedder dimensions through the factory so the vector
        # store can size itself correctly.
        if self._embedder is not None:
            config.setdefault(
                "EMBEDDING_DIMENSIONS",
                str(getattr(self._embedder, "dimensions", 0) or 0),
            )

        self._store, self._vector_store = create_stores(config)
        self._engine = EngramaEngine(
            self._store,
            vector_store=self._vector_store,
            embedder=self._embedder,
            default_provenance=Provenance(
                source="sdk",
                source_agent=source_agent,
                source_session=source_session,
            ),
        )

        # Skills
        self._remember = RememberSkill()
        self._recall = RecallSkill()
        self._associate = AssociateSkill()
        self._forget = ForgetSkill()
        self._reflect = ReflectSkill()
        self._proactive = ProactiveSkill()

        # Optional Obsidian adapter
        self._obsidian = None
        try:
            from engrama.adapters.obsidian import ObsidianAdapter

            self._obsidian = ObsidianAdapter(vault_path=vault_path)
        except (FileNotFoundError, ImportError):
            pass

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> Engrama:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying store (file handle / driver)."""
        if hasattr(self._store, "close"):
            self._store.close()

    def verify(self) -> None:
        """Check the configured backend is reachable."""
        if hasattr(self._store, "health_check"):
            self._store.health_check()

    # ------------------------------------------------------------------
    # Remember
    # ------------------------------------------------------------------

    def remember(
        self,
        label: str,
        name: str,
        observation: str,
        **extra: Any,
    ) -> dict:
        """Create or update a node with an observation.

        Args:
            label: Node label (e.g. ``"Project"``, ``"Technology"``).
            name: Node identity (name or title depending on label).
            observation: Free-text observation to store.
            **extra: Additional properties to set on the node.

        Returns:
            Dict with ``label``, ``key``, ``name``, ``created``.
        """
        return self._remember.run(
            self._engine,
            label=label,
            name=name,
            observation=observation,
            extra=extra or None,
        )

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        hops: int = 2,
    ) -> list[RecallResult]:
        """Search the graph and expand each hit with its neighbourhood.

        Args:
            query: Search string.
            limit: Max seed nodes from fulltext search.
            hops: Neighbourhood expansion depth.

        Returns:
            List of :class:`RecallResult` with properties and neighbours.
        """
        return self._recall.run(self._engine, query=query, limit=limit, hops=hops)

    # ------------------------------------------------------------------
    # Search (raw fulltext, no expansion)
    # ------------------------------------------------------------------

    def search(self, query: str, *, limit: int = 10) -> list[dict]:
        """Run a raw fulltext search without graph expansion.

        Args:
            query: Lucene-syntax search string.
            limit: Max results.

        Returns:
            List of dicts with ``type``, ``name``, ``score``.
        """
        records = self._engine.search(query, limit=limit)
        return [{"type": r["type"], "name": r["name"], "score": r["score"]} for r in records]

    def hybrid_search(self, query: str, *, limit: int = 10) -> list[dict]:
        """Run a hybrid search combining fulltext and vector similarity.

        Falls back to plain fulltext when embeddings are not configured.

        Args:
            query: Natural-language search string.
            limit: Max results.

        Returns:
            List of dicts with ``type``, ``name``, ``score``,
            ``vector_score``, ``fulltext_score``.
        """
        results = self._engine.hybrid_search(query, limit=limit)
        return [
            {
                "type": r.label,
                "name": r.name,
                "score": round(r.final_score, 4),
                "vector_score": round(r.vector_score, 4),
                "fulltext_score": round(r.fulltext_score, 4),
            }
            for r in results
        ]

    # ------------------------------------------------------------------
    # Associate
    # ------------------------------------------------------------------

    def associate(
        self,
        from_name: str,
        from_label: str,
        rel_type: str,
        to_name: str,
        to_label: str,
    ) -> dict:
        """Create a typed relationship between two nodes.

        DDR-002: If an Obsidian adapter is available, the relation is also
        written to the source note's YAML frontmatter (dual-write).

        Args:
            from_name: Source node identity.
            from_label: Source node label.
            rel_type: Relationship type (e.g. ``"USES"``).
            to_name: Target node identity.
            to_label: Target node label.

        Returns:
            Dict with ``matched`` (bool), ``vault_written`` (bool),
            and relationship details.
        """
        return self._associate.run(
            self._engine,
            from_name=from_name,
            from_label=from_label,
            rel_type=rel_type,
            to_name=to_name,
            to_label=to_label,
            obsidian=self._obsidian,
        )

    # ------------------------------------------------------------------
    # Forget
    # ------------------------------------------------------------------

    def forget(
        self,
        label: str,
        name: str,
        *,
        purge: bool = False,
    ) -> dict:
        """Archive (or delete) a specific node.

        Args:
            label: Node label.
            name: Node identity.
            purge: ``True`` to permanently delete. Default soft-archives.

        Returns:
            Dict with ``action`` and ``matched``.
        """
        return self._forget.forget_by_name(self._engine, label=label, name=name, purge=purge)

    def forget_by_ttl(
        self,
        label: str,
        *,
        days: int,
        purge: bool = False,
    ) -> dict:
        """Archive (or delete) nodes older than a threshold.

        Args:
            label: Node label to scan.
            days: Age threshold in days.
            purge: ``True`` to permanently delete. Default soft-archives.

        Returns:
            Dict with ``action`` and ``count``.
        """
        return self._forget.forget_by_ttl(self._engine, label=label, days=days, purge=purge)

    # ------------------------------------------------------------------
    # Decay (DDR-003 Phase D)
    # ------------------------------------------------------------------

    def decay_scores(
        self,
        *,
        rate: float = 0.01,
        min_confidence: float = 0.0,
        max_age_days: int = 0,
        label: str | None = None,
    ) -> dict:
        """Apply exponential confidence decay to all nodes.

        Args:
            rate: Decay rate (0.01 ≈ 63 % after 100 days).
            min_confidence: Archive nodes below this threshold.
            max_age_days: Archive nodes older than this.
            label: Restrict to a specific label.

        Returns:
            Dict with ``decayed`` and ``archived`` counts.
        """
        return self._engine.decay_scores(
            rate=rate,
            min_confidence=min_confidence,
            max_age_days=max_age_days,
            label=label,
        )

    # ------------------------------------------------------------------
    # Reflect
    # ------------------------------------------------------------------

    def reflect(self) -> list:
        """Run cross-entity pattern detection and write Insight nodes.

        Returns:
            List of :class:`Insight` dataclasses created or updated.
        """
        return self._reflect.run(self._engine)

    # ------------------------------------------------------------------
    # Proactive (Insight lifecycle)
    # ------------------------------------------------------------------

    def surface_insights(self, *, limit: int = 10) -> list[SurfacedInsight]:
        """Read pending Insights for human review.

        Returns:
            List of :class:`SurfacedInsight` newest first.
        """
        return self._proactive.surface(self._engine, limit=limit)

    def approve_insight(self, title: str) -> dict:
        """Mark an Insight as approved."""
        return self._proactive.approve(self._engine, title=title)

    def dismiss_insight(self, title: str) -> dict:
        """Mark an Insight as dismissed."""
        return self._proactive.dismiss(self._engine, title=title)

    def write_insight_to_vault(self, title: str, target_note: str) -> dict:
        """Append an approved Insight to an Obsidian note.

        Raises:
            RuntimeError: If Obsidian adapter is not available.
        """
        if self._obsidian is None:
            raise RuntimeError(
                "Obsidian adapter not available. Set VAULT_PATH or pass "
                "vault_path to the Engrama constructor."
            )
        return self._proactive.write_to_vault(
            self._engine,
            self._obsidian,
            title=title,
            target_note=target_note,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def has_vault(self) -> bool:
        """Whether the Obsidian adapter is connected."""
        return self._obsidian is not None

    def __repr__(self) -> str:
        vault = f", vault={self._obsidian.vault_path}" if self._obsidian else ""
        backend = type(self._store).__name__
        return f"Engrama(backend={backend}{vault})"


__all__ = ["Engrama"]
