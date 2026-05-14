"""Security and provenance primitives (DDR-003 Phase E).

This module exposes:

* :class:`Provenance` — a frozen dataclass tagging every write with
  where it came from (PR-E1, layer 2 of DDR-003 Part 5).
* :class:`Sanitiser` — strips dangerous content and enforces the
  node/relation schema before any write reaches a store (PR-E2,
  layer 1 of DDR-003 Part 5).

The trust-aware retrieval piece (layer 3) lands in PR-E3.

Provenance is persisted as four flat properties on the node so it flows
through the existing ``GraphStore.merge_node`` contract without any
backend changes:

* ``source`` — broad origin bucket (``"mcp" | "sdk" | "cli" | "sync"``).
* ``source_agent`` — optional agent identifier.
* ``source_session`` — optional session identifier.
* ``trust_level`` — float in ``[0.0, 1.0]``, defaults derived from source.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("engrama.core.security")

DEFAULT_TRUST_LEVELS: dict[str, float] = {
    "sync": 1.0,
    "cli": 1.0,
    "sdk": 0.8,
    "mcp": 0.5,
}

# Keys that may NEVER be supplied by a caller's property bag — they must
# come from a :class:`Provenance` instance applied by the engine or
# adapter. Otherwise a malicious agent could spoof its own trust_level
# or source bucket.
RESERVED_PROVENANCE_KEYS: frozenset[str] = frozenset(
    {"source", "source_agent", "source_session", "trust_level"}
)

# Keys that may never be supplied by a caller's property bag — they must
# come from a :class:`~engrama.core.scope.MemoryScope` instance applied
# by the engine. Otherwise a malicious agent could relocate its own
# writes into another user's or org's scope.
RESERVED_SCOPE_KEYS: frozenset[str] = frozenset({"org_id", "user_id", "agent_id", "session_id"})

# Union of every reserved key — used by the sanitiser to strip system-
# managed fields from caller-supplied property bags in one pass.
RESERVED_KEYS: frozenset[str] = RESERVED_PROVENANCE_KEYS | RESERVED_SCOPE_KEYS

# Maximum string length for a single property value (in characters).
# Strings longer than this are truncated with a logged warning — belt-
# and-suspenders against memory exhaustion and against using a property
# as a covert exfiltration channel through outsized payloads.
MAX_PROPERTY_VALUE_LEN: int = 100_000


def default_trust_for(source: str) -> float:
    """Return the default ``trust_level`` for a given ``source`` bucket.

    Looks up ``ENGRAMA_TRUST_LEVELS`` first (comma-separated
    ``source=value`` pairs, e.g. ``"sync=1.0,cli=1.0,sdk=0.9,mcp=0.3"``)
    so operators can tighten or loosen the defaults without code changes.
    Falls back to :data:`DEFAULT_TRUST_LEVELS`. Unknown sources get
    ``0.5`` — a neutral middle.

    Invalid entries (non-float values or values outside ``[0.0, 1.0]``)
    are skipped with a logged warning so the operator can see what was
    rejected — silently dropping bad values would let a typo like
    ``mcp=99`` distort ranking without anyone noticing.
    """
    raw = os.environ.get("ENGRAMA_TRUST_LEVELS")
    if raw:
        overrides: dict[str, float] = {}
        for part in raw.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key = key.strip()
            value = value.strip()
            try:
                parsed = float(value)
            except ValueError:
                logger.warning(
                    "Ignoring ENGRAMA_TRUST_LEVELS entry %r: %r is not a float",
                    key,
                    value,
                )
                continue
            if not 0.0 <= parsed <= 1.0:
                logger.warning(
                    "Ignoring ENGRAMA_TRUST_LEVELS entry %r=%s: value must be in [0.0, 1.0]",
                    key,
                    parsed,
                )
                continue
            overrides[key] = parsed
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


class Sanitiser:
    """Layer-1 defence: clean values, strip reserved keys, enforce schema.

    Applied at the engine boundary on every write **before** provenance
    is mixed in, so a caller's property bag cannot smuggle a
    higher-trust ``source`` or a non-schema ``label``. The MCP adapter
    runs the same sanitiser at its own boundary because it talks to the
    store directly (bypassing the engine).

    Construct with the project's default whitelists from
    :mod:`engrama.core.schema`; tests can pass narrower sets.
    """

    def __init__(
        self,
        valid_labels: set[str] | None = None,
        valid_relations: set[str] | None = None,
        *,
        max_value_len: int = MAX_PROPERTY_VALUE_LEN,
    ) -> None:
        if valid_labels is None or valid_relations is None:
            from engrama.core.schema import NodeType, RelationType

            if valid_labels is None:
                valid_labels = {member.value for member in NodeType}
            if valid_relations is None:
                valid_relations = {member.value for member in RelationType}
        self.valid_labels: set[str] = set(valid_labels)
        self.valid_relations: set[str] = set(valid_relations)
        self.max_value_len: int = max_value_len

    def sanitise_properties(self, props: dict[str, Any]) -> dict[str, Any]:
        """Return a new dict with reserved keys removed and values cleaned.

        Filters:
        - Drop any key in :data:`RESERVED_KEYS` (system-managed
          provenance and scope dimensions).
        - Drop any key starting with ``_`` (reserved for internal use).
        - Clean every value: truncate long strings, strip C0 control
          characters except tab/newline, recurse through ``list`` / ``dict``.
        """
        out: dict[str, Any] = {}
        for key, value in props.items():
            if key in RESERVED_KEYS:
                continue
            if isinstance(key, str) and key.startswith("_"):
                continue
            out[key] = self._clean_value(value)
        return out

    def validate_label(self, label: str) -> str:
        """Return ``label`` unchanged if in the whitelist; else ``ValueError``."""
        if label not in self.valid_labels:
            raise ValueError(f"Unknown node label: {label!r}")
        return label

    def validate_relation(self, rel_type: str) -> str:
        """Return ``rel_type`` unchanged if in the whitelist; else ``ValueError``."""
        if rel_type not in self.valid_relations:
            raise ValueError(f"Unknown relation type: {rel_type!r}")
        return rel_type

    def _clean_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._clean_string(value)
        if isinstance(value, list):
            return [self._clean_value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._clean_value(v) for v in value)
        if isinstance(value, dict):
            return {k: self._clean_value(v) for k, v in value.items()}
        return value

    def _clean_string(self, s: str) -> str:
        if len(s) > self.max_value_len:
            logger.warning(
                "Property value truncated from %d to %d chars",
                len(s),
                self.max_value_len,
            )
            s = s[: self.max_value_len]
        # Strip C0 (0x00-0x1F) and DEL (0x7F) control characters,
        # preserving TAB (0x09) and LF (0x0A) which are legitimate
        # whitespace inside multi-line text fields.
        return "".join(c for c in s if c in ("\t", "\n") or not (ord(c) < 0x20 or ord(c) == 0x7F))


__all__ = [
    "DEFAULT_TRUST_LEVELS",
    "MAX_PROPERTY_VALUE_LEN",
    "Provenance",
    "RESERVED_KEYS",
    "RESERVED_PROVENANCE_KEYS",
    "RESERVED_SCOPE_KEYS",
    "Sanitiser",
    "default_trust_for",
]
