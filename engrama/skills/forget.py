"""
engrama/skills/forget.py

The forget skill archives or removes nodes from the memory graph.  Two
modes are supported:

1. **By name** — archive a specific node by setting ``status: "archived"``
   and ``archived_at`` timestamp.  The node stays in the graph but is
   excluded from active queries.
2. **By TTL** — find and archive all nodes of a given label whose
   ``updated_at`` is older than a threshold.

Archiving (soft-delete) is preferred over hard-delete to preserve graph
history.  A ``purge`` flag can be set to actually DETACH DELETE the node
when the caller explicitly wants permanent removal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine


class ForgetSkill:
    """Archive or remove nodes from the memory graph."""

    def forget_by_name(
        self,
        engine: "EngramaEngine",
        *,
        label: str,
        name: str,
        purge: bool = False,
    ) -> dict:
        """Archive (or delete) a specific node by its identity.

        Args:
            engine: An initialised :class:`EngramaEngine`.
            label: Neo4j node label.
            name: The node's identity value (name or title).
            purge: If ``True``, permanently ``DETACH DELETE`` the node.
                   If ``False`` (default), set ``status: "archived"``
                   and ``archived_at: datetime()``.

        Returns:
            A dict with ``label``, ``name``, ``action`` (``"archived"``
            or ``"deleted"``), and ``matched`` (bool).
        """
        result = engine._store.archive_node_by_name(label, name, purge=purge)
        return {
            "label": label,
            "name": name,
            "action": "deleted" if purge else "archived",
            "matched": result["matched"],
        }

    def forget_by_ttl(
        self,
        engine: "EngramaEngine",
        *,
        label: str,
        days: int,
        purge: bool = False,
    ) -> dict:
        """Archive (or delete) all nodes of a label older than *days*.

        A node is considered stale when its ``updated_at`` is more than
        *days* days in the past.  Nodes without ``updated_at`` are skipped.

        Args:
            engine: An initialised :class:`EngramaEngine`.
            label: Neo4j node label to scan.
            days: Age threshold in days.
            purge: If ``True``, permanently delete.  Default archives.

        Returns:
            A dict with ``label``, ``days``, ``action``, ``count``.
        """
        if days < 1:
            raise ValueError("days must be >= 1")

        result = engine._store.archive_nodes_older_than(label, days, purge=purge)

        return {
            "label": label,
            "days": days,
            "action": "deleted" if purge else "archived",
            "count": result["affected"],
        }
