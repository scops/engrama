"""Security and provenance primitives (DDR-003 Phase E).

This module currently exposes the :class:`Provenance` dataclass that the
engine, SDK, MCP server and Obsidian sync layer use to tag every write
with where it came from. The sanitiser and trust-aware retrieval pieces
listed in DDR-003 Part 5 land in follow-up PRs (E2 and E3).

Provenance is persisted as four flat properties on the node so it flows
through the existing ``GraphStore.merge_node`` contract without any
backend changes:

* ``source`` — broad origin bucket (``"mcp" | "sdk" | "cli" | "sync"``).
* ``source_agent`` — optional agent identifier.
* ``source_session`` — optional session identifier.
* ``trust_level`` — float in ``[0.0, 1.0]``, defaults derived from source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TRUST_LEVELS: dict[str, float] = {
    "sync": 1.0,
    "cli": 1.0,
    "sdk": 0.8,
    "mcp": 0.5,
}


def default_trust_for(source: str) -> float:
    """Return the default ``trust_level`` for a given ``source`` bucket.

    Looks up ``ENGRAMA_TRUST_LEVELS`` first (comma-separated
    ``source=value`` pairs, e.g. ``"sync=1.0,cli=1.0,sdk=0.9,mcp=0.3"``)
    so operators can tighten or loosen the defaults without code changes.
    Falls back to :data:`DEFAULT_TRUST_LEVELS`. Unknown sources get
    ``0.5`` — a neutral middle.
    """
    raw = os.environ.get("ENGRAMA_TRUST_LEVELS")
    if raw:
        overrides: dict[str, float] = {}
        for part in raw.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            try:
                overrides[key.strip()] = float(value.strip())
            except ValueError:
                continue
        if source in overrides:
            return overrides[source]
    return DEFAULT_TRUST_LEVELS.get(source, 0.5)


@dataclass(frozen=True)
class Provenance:
    """Where a write to the graph came from.

    Flattens to four properties via :meth:`to_properties` and is merged
    into the node's property bag inside the engine (or, for direct
    store calls in the MCP server, via a local helper). ``trust_level``
    is auto-filled from ``source`` when left as ``None``.

    Frozen so callers can't mutate it after handing it off across layers.
    """

    source: str
    source_agent: str | None = None
    source_session: str | None = None
    trust_level: float | None = field(default=None)

    def __post_init__(self) -> None:
        if self.trust_level is None:
            object.__setattr__(self, "trust_level", default_trust_for(self.source))

    def to_properties(self) -> dict[str, Any]:
        """Return a dict of the non-``None`` fields suitable for ``merge_node``."""
        out: dict[str, Any] = {"source": self.source, "trust_level": self.trust_level}
        if self.source_agent is not None:
            out["source_agent"] = self.source_agent
        if self.source_session is not None:
            out["source_session"] = self.source_session
        return out


__all__ = ["DEFAULT_TRUST_LEVELS", "Provenance", "default_trust_for"]
