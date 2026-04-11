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

from engrama.core.schema import TITLE_KEYED_LABELS

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
        merge_key = "title" if label in TITLE_KEYED_LABELS else "name"

        if purge:
            query = (
                f"MATCH (n:{label} {{{merge_key}: $name}}) "
                "DETACH DELETE n "
                "RETURN count(*) AS deleted"
            )
            records = engine._client.run(query, {"name": name})
            deleted = records[0]["deleted"] if records else 0
            return {
                "label": label,
                "name": name,
                "action": "deleted",
                "matched": deleted > 0,
            }
        else:
            query = (
                f"MATCH (n:{label} {{{merge_key}: $name}}) "
                "SET n.status = 'archived', n.archived_at = datetime(), "
                "    n.updated_at = datetime() "
                "RETURN n"
            )
            records = engine._client.run(query, {"name": name})
            return {
                "label": label,
                "name": name,
                "action": "archived",
                "matched": len(records) > 0,
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

        if purge:
            query = (
                f"MATCH (n:{label}) "
                "WHERE n.updated_at IS NOT NULL "
                "  AND n.updated_at < datetime() - duration({days: $days}) "
                "DETACH DELETE n "
                "RETURN count(*) AS affected"
            )
        else:
            query = (
                f"MATCH (n:{label}) "
                "WHERE n.updated_at IS NOT NULL "
                "  AND n.updated_at < datetime() - duration({days: $days}) "
                "  AND (n.status IS NULL OR n.status <> 'archived') "
                "SET n.status = 'archived', n.archived_at = datetime(), "
                "    n.updated_at = datetime() "
                "RETURN count(n) AS affected"
            )

        records = engine._client.run(query, {"days": days})
        count = records[0]["affected"] if records else 0

        return {
            "label": label,
            "days": days,
            "action": "deleted" if purge else "archived",
            "count": count,
        }
