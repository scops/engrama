"""
engrama/skills/remember.py

The remember skill writes a new observation to an existing or new node in the
memory graph.  It is the primary "save this" entry point for agents.

Behaviour:

* **Existing node** — the observation is appended to ``notes`` (or the
  profile-appropriate free-text property) via MERGE + ON MATCH SET.
* **New node** — a minimal node is created with the merge key + notes.
* Timestamps are managed by :meth:`EngramaEngine.merge_node`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engrama.core.schema import TITLE_KEYED_LABELS

if TYPE_CHECKING:
    from engrama.core.engine import EngramaEngine


class RememberSkill:
    """Write an observation to the memory graph.

    Wraps :meth:`EngramaEngine.merge_node` with a friendlier interface
    that auto-detects the merge key (``name`` vs ``title``) based on the
    node label.
    """

    def run(
        self,
        engine: EngramaEngine,
        *,
        label: str,
        name: str,
        observation: str,
        extra: dict | None = None,
    ) -> dict:
        """Create or update a node with the given observation.

        Args:
            engine: An initialised :class:`EngramaEngine`.
            label: Neo4j node label (e.g. ``"Project"``, ``"Decision"``).
            name: The node's identity value (name or title depending on label).
            observation: Free-text observation to store in ``notes``.
            extra: Optional additional properties to set on the node.

        Returns:
            A dict with ``label``, ``key``, ``name``, ``created`` (bool).
        """
        merge_key = "title" if label in TITLE_KEYED_LABELS else "name"

        props: dict = {merge_key: name, "notes": observation}
        if extra:
            props.update(extra)

        records = engine.merge_node(label, props)

        # Determine if the node was freshly created by checking whether
        # created_at == updated_at (both set to datetime() on CREATE).
        created = False
        if records:
            node = records[0]["n"]
            created = node.get("created_at") == node.get("updated_at")

        return {
            "label": label,
            "key": merge_key,
            "name": name,
            "created": created,
        }
