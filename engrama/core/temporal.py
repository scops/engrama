"""
Engrama — Temporal reasoning (DDR-003 Phase D).

Provides confidence decay, temporal scoring, and conflict detection
for memory nodes.  All calculations are pure Python — database queries
live in the backends.

Decay model (exponential):

    confidence(t) = confidence_0 * exp(-rate * days_since_update)

Where:
    - ``confidence_0`` is the node's stored confidence (default 1.0)
    - ``rate`` controls how fast old memories fade
    - ``days_since_update`` is ``(now - updated_at).days``
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class DecayConfig:
    """Parameters for confidence decay.

    Attributes:
        rate: Exponential decay rate.  ``0.01`` ≈ 63 % confidence
            after 100 days.  ``0.005`` is gentler.
        min_confidence: Nodes below this threshold can be auto-archived.
            ``0.0`` means never auto-archive.
        max_age_days: Nodes older than this (by ``updated_at``) can be
            auto-archived regardless of confidence.  ``0`` means no
            age limit.
    """

    rate: float = 0.01
    min_confidence: float = 0.0
    max_age_days: int = 0


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def compute_decayed_confidence(
    confidence: float,
    days_since_update: float,
    rate: float = 0.01,
) -> float:
    """Return the decayed confidence for a node.

    Args:
        confidence: Current stored confidence (0.0–1.0).
        days_since_update: Age in fractional days since ``updated_at``.
        rate: Exponential decay rate.

    Returns:
        Decayed confidence, clamped to ``[0.0, 1.0]``.
    """
    if days_since_update <= 0 or rate <= 0:
        return max(0.0, min(1.0, confidence))
    decayed = confidence * math.exp(-rate * days_since_update)
    return max(0.0, min(1.0, decayed))


def temporal_score(
    confidence: float,
    days_since_update: float,
    *,
    recency_half_life: float = 30.0,
) -> float:
    """Compute a temporal relevance score for search ranking.

    Combines stored confidence with a recency signal.
    Returns a value in ``[0.0, 1.0]``.

    The recency component uses the same exponential model:
    ``recency = exp(-ln2 / half_life * days)`` so that a node updated
    ``half_life`` days ago scores 0.5 on recency.

    The final score is ``confidence * recency`` — both must be high
    for a node to rank well temporally.
    """
    if days_since_update <= 0:
        recency = 1.0
    else:
        recency = math.exp(-math.log(2) / recency_half_life * days_since_update)
    return max(0.0, min(1.0, confidence * recency))


def days_since(dt: datetime | str | None) -> float:
    """Return fractional days between *dt* and now (UTC).

    Accepts ISO-format strings (common from Neo4j datetime serialisation)
    or ``datetime`` objects.  Returns ``0.0`` if *dt* is ``None``.
    """
    if dt is None:
        return 0.0
    if isinstance(dt, str):
        # Neo4j ISO strings may have timezone info or not
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    # Neo4j driver returns neo4j.time.DateTime — convert to stdlib
    if hasattr(dt, "to_native"):
        dt = dt.to_native()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    return max(0.0, delta.total_seconds() / 86400.0)


def detect_conflict(node_props: dict) -> dict | None:
    """Check if a node that is being re-merged was previously expired.

    A node is "expired" when it has a ``valid_to`` timestamp that is in
    the past.  Re-merging it means the knowledge is being revived —
    callers should clear ``valid_to`` and raise the confidence.

    Returns:
        ``None`` if no conflict; otherwise a dict with details:
        ``{"conflict": "revived", "previous_valid_to": ..., "action": "cleared"}``.
    """
    valid_to = node_props.get("valid_to")
    if valid_to is None:
        return None

    # Parse and compare
    if isinstance(valid_to, str):
        vt = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
    elif isinstance(valid_to, datetime):
        vt = valid_to
    else:
        return None

    if vt.tzinfo is None:
        vt = vt.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    if vt < now:
        return {
            "conflict": "revived",
            "previous_valid_to": valid_to,
            "action": "cleared",
        }
    return None
