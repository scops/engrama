"""Stub lifecycle (DDR-006 item 6).

A stub is a placeholder node minted when an inline relation points at a name
that doesn't exist yet (``status: "stub"``). It stays a stub only until
someone writes real content into it: a write that supplies ``summary`` or
``details`` without stating a status promotes it to ``active``. The stores
apply this on update, so it holds for every write path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

STUB_STATUS = "stub"
ENRICHING_FIELDS: tuple[str, ...] = ("summary", "details")
# A stub with at least this many edges is structural: worth enriching.
HUB_STUB_MIN_DEGREE = 3


def clears_stub(properties: Mapping[str, Any]) -> bool:
    """Whether this write enriches a stub enough to promote it to ``active``."""
    return "status" not in properties and any(properties.get(f) for f in ENRICHING_FIELDS)


__all__ = ["ENRICHING_FIELDS", "HUB_STUB_MIN_DEGREE", "STUB_STATUS", "clears_stub"]
