"""
engrama/skills/associate.py

The associate skill creates relationships between existing nodes in the
memory graph.  It is the primary "link A to B" entry point for agents.

DDR-002: When a vault adapter is available, every relation created in the
graph is also written to the source note's YAML frontmatter (dual-write).

Validation:

* Both endpoint labels must exist in :class:`~engrama.core.schema.NodeType`.
* The relationship type must exist in :class:`~engrama.core.schema.RelationType`.
* If either endpoint node does not exist in Neo4j, the relationship is
  silently not created (MERGE requires both MATCHes to succeed).

The skill delegates to :meth:`EngramaEngine.merge_relation`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from engrama.core.schema import NodeType, RelationType

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine
    from engrama.adapters.obsidian.adapter import ObsidianAdapter

logger = logging.getLogger(__name__)


class AssociateSkill:
    """Create a typed relationship between two nodes.

    Validates labels and relationship types against the schema before
    calling :meth:`EngramaEngine.merge_relation`.

    When an :class:`ObsidianAdapter` is provided, also writes the relation
    to the source note's frontmatter (DDR-002 dual-write contract).
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
        obsidian: "ObsidianAdapter | None" = None,
    ) -> dict:
        """Create or update a relationship between two nodes.

        Args:
            engine: An initialised :class:`EngramaEngine`.
            from_name: Identity value of the source node.
            from_label: Label of the source node (e.g. ``"Project"``).
            rel_type: Relationship type (e.g. ``"USES"``).
            to_name: Identity value of the target node.
            to_label: Label of the target node.
            obsidian: Optional :class:`ObsidianAdapter` for dual-write to
                      vault frontmatter (DDR-002).

        Returns:
            A dict with ``from_name``, ``rel_type``, ``to_name``, ``matched``
            (bool — True if both endpoints existed and the rel was created),
            and ``vault_written`` (bool — True if the relation was also
            written to the vault frontmatter).

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

        # --- Execute: graph write ---
        records = engine.merge_relation(
            from_name=from_name,
            from_label=from_label,
            rel_type=rel_type,
            to_name=to_name,
            to_label=to_label,
        )

        matched = len(records) > 0

        # --- DDR-002: vault dual-write ---
        vault_written = False
        if matched and obsidian is not None:
            vault_written = self._write_relation_to_vault(
                engine, obsidian,
                from_name=from_name,
                from_label=from_label,
                rel_type=rel_type,
                to_name=to_name,
            )

        return {
            "from_name": from_name,
            "from_label": from_label,
            "rel_type": rel_type,
            "to_name": to_name,
            "to_label": to_label,
            "matched": matched,
            "vault_written": vault_written,
        }

    @staticmethod
    def _write_relation_to_vault(
        engine: "EngramaEngine",
        obsidian: "ObsidianAdapter",
        *,
        from_name: str,
        from_label: str,
        rel_type: str,
        to_name: str,
    ) -> bool:
        """Write a relation to the source node's Obsidian note frontmatter.

        Looks up the source node's ``obsidian_path`` property in the graph,
        then calls ``adapter.add_relation()`` to append to the frontmatter.

        Returns True if the vault was updated.
        """
        try:
            vault_path = engine._store.find_obsidian_path(from_label, from_name)
            if not vault_path:
                return False
            return obsidian.add_relation(vault_path, rel_type, to_name)
        except Exception as e:
            logger.warning(
                "DDR-002 vault write failed for %s -[%s]-> %s: %s",
                from_name, rel_type, to_name, e,
            )
            return False
