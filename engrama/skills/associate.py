"""
engrama/skills/associate.py

The associate skill creates relationships between existing nodes in the
memory graph.  It is the primary "link A to B" entry point for agents.

Validation:

* Both endpoint labels must exist in :class:`~engrama.core.schema.NodeType`.
* The relationship type must exist in :class:`~engrama.core.schema.RelationType`.
* If either endpoint node does not exist in Neo4j, the relationship is
  silently not created (MERGE requires both MATCHes to succeed).

The skill delegates to :meth:`EngramaEngine.merge_relation`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engrama.core.schema import NodeType, RelationType, TITLE_KEYED_LABELS

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine


class AssociateSkill:
    """Create a typed relationship between two nodes.

    Validates labels and relationship types against the schema before
    calling :meth:`EngramaEngine.merge_relation`.
    """

    # Pre-compute valid values for fast lookups.
    _VALID_LABELS: frozenset[str] = frozenset(m.value for m in NodeType)
    _VALID_RELS: frozenset[str] = frozenset(m.value for m in RelationType)

    def run(
        self,
        engine: "EngramaEngine",
        *,
        from_name: str,
        from_label: str,
        rel_type: str,
        to_name: str,
        to_label: str,
    ) -> dict:
        """Create or update a relationship between two nodes.

        Args:
            engine: An initialised :class:`EngramaEngine`.
            from_name: Identity value of the source node.
            from_label: Label of the source node (e.g. ``"Project"``).
            rel_type: Relationship type (e.g. ``"USES"``).
            to_name: Identity value of the target node.
            to_label: Label of the target node.

        Returns:
            A dict with ``from_name``, ``rel_type``, ``to_name``, ``matched``
            (bool — True if both endpoints existed and the rel was created).

        Raises:
            ValueError: If any label or relationship type is not in the schema.
        """
        # --- Validation ---
        if from_label not in self._VALID_LABELS:
            raise ValueError(
                f"Unknown source label {from_label!r}. "
                f"Valid: {sorted(self._VALID_LABELS)}"
            )
        if to_label not in self._VALID_LABELS:
            raise ValueError(
                f"Unknown target label {to_label!r}. "
                f"Valid: {sorted(self._VALID_LABELS)}"
            )
        if rel_type not in self._VALID_RELS:
            raise ValueError(
                f"Unknown relationship type {rel_type!r}. "
                f"Valid: {sorted(self._VALID_RELS)}"
            )

        # --- Execute ---
        records = engine.merge_relation(
            from_name=from_name,
            from_label=from_label,
            rel_type=rel_type,
            to_name=to_name,
            to_label=to_label,
        )

        return {
            "from_name": from_name,
            "from_label": from_label,
            "rel_type": rel_type,
            "to_name": to_name,
            "to_label": to_label,
            "matched": len(records) > 0,
        }
