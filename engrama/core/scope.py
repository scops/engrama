"""Scope model for multi-scope memory (DDR-003 Phase F / Roadmap P14).

This module defines :class:`MemoryScope` — the four-dimension address
that locates a write inside the org → user → agent → session hierarchy.
PR-F1 carries the scope through writes (every node tagged with the
dimensions that are set on the active scope). PR-F2 will apply the
same scope on the read side as a query filter.

Per DDR-003 Part 6, for v1 Engrama stays single-user: every dimension
defaults to ``None``, ``MemoryScope().to_properties()`` is empty, and
the node carries no scope properties. Switching to multi-user is an
operator decision (set the dimensions when constructing the engine
or the SDK), not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MemoryScope:
    """The four-dimensional scope of a memory operation.

    Hierarchy from broadest to narrowest::

        org_id (broadest)
          └── user_id
                └── agent_id
                      └── session_id (narrowest)

    Dimensions left as ``None`` are unscoped — for v1 single-user
    deployments every field defaults to ``None``, which is equivalent to
    "no scope" and means writes carry no scope properties and reads
    apply no scope filter.

    Frozen so callers can't mutate a scope after handing it off across
    layers.
    """

    org_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None

    def to_properties(self) -> dict[str, Any]:
        """Return the non-``None`` dimensions as a flat property dict.

        Empty dict when every dimension is ``None`` — the engine then
        adds nothing to the node, preserving the single-user default.
        """
        out: dict[str, Any] = {}
        if self.org_id is not None:
            out["org_id"] = self.org_id
        if self.user_id is not None:
            out["user_id"] = self.user_id
        if self.agent_id is not None:
            out["agent_id"] = self.agent_id
        if self.session_id is not None:
            out["session_id"] = self.session_id
        return out

    def is_empty(self) -> bool:
        """``True`` iff every dimension is ``None`` (no-op scope)."""
        return (
            self.org_id is None
            and self.user_id is None
            and self.agent_id is None
            and self.session_id is None
        )


__all__ = ["MemoryScope"]
