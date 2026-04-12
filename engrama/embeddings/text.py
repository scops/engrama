"""
Engrama — Text representation for embedding.

Converts a node (label + properties) into a single text string suitable
for embedding.  This is the canonical way to produce the text that gets
embedded — all providers use it, ensuring consistent vector
representations regardless of which model generates the embedding.
"""

from __future__ import annotations

from typing import Any


# Properties that carry meaningful free-text content, in priority order.
_TEXT_PROPERTIES: tuple[str, ...] = (
    "description",
    "notes",
    "rationale",
    "solution",
    "context",
    "body",
)


def node_to_text(label: str, props: dict[str, Any]) -> str:
    """Build a text representation of a node for embedding.

    Format::

        Label: name_or_title description notes rationale solution context body

    Empty or missing properties are silently skipped.

    Args:
        label: The node's Neo4j label (e.g. ``"Project"``).
        props: The node's property dict.

    Returns:
        A single string ready to pass to an ``EmbeddingProvider``.

    Example::

        >>> node_to_text("Project", {"name": "engrama", "description": "Memory graph"})
        'Project: engrama Memory graph'
    """
    parts: list[str] = [f"{label}:"]

    # Primary identity — name or title
    identity = props.get("name") or props.get("title", "")
    if identity:
        parts.append(str(identity))

    # Free-text content properties
    for field in _TEXT_PROPERTIES:
        value = props.get(field)
        if value:
            parts.append(str(value))

    return " ".join(parts)
