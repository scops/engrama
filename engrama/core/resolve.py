"""Entity resolution on write (DDR-006 items 1-3).

Decides, for a name about to be written, whether it refers to a node that
already exists. The stores only supply candidates from bounded lookups
(``name_candidates``: in-scope nodes whose name shares a fragment with the
target; vector kNN for the remembered node); everything here is pure, so the
policy is the same on every backend and testable without a database.

Two consumers:

* **Inline relation targets** (:func:`decide_target`): connect to an existing
  node only on near-certainty; otherwise ask (``did_you_mean``) or create a
  stub. An exact match under another label, or on an archived node, is never
  connected silently.
* **The remembered node itself** (:func:`possible_duplicates`): when a *new*
  node looks like an existing one (same name under another label, a very
  similar name, or a near-identical embedding), the write reports it.
  Semantic similarity only ever warns, never connects.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from engrama.core.names import normalise_key

CONNECT_MARGIN = 0.08
SUGGEST_LIMIT = 5
DUPLICATE_LIMIT = 5
_DEDUPE_MODES = ("off", "warn", "block")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def connect_ratio() -> float:
    """Lexical similarity needed to connect without asking."""
    return _env_float("ENGRAMA_RESOLVE_CONNECT", 0.9)


def suggest_ratio() -> float:
    """Lexical similarity needed to appear in ``did_you_mean``."""
    return _env_float("ENGRAMA_RESOLVE_SUGGEST", 0.6)


def duplicate_ratio() -> float:
    """Lexical similarity for a new node to be reported as a possible duplicate."""
    return _env_float("ENGRAMA_RESOLVE_DUPLICATE", 0.85)


def duplicate_cosine() -> float:
    """Embedding cosine for a new node to be reported as a possible duplicate."""
    return _env_float("ENGRAMA_RESOLVE_VECTOR", 0.9)


def dedupe_mode() -> str:
    """``ENGRAMA_REMEMBER_DEDUPE``: ``off`` | ``warn`` (default) | ``block``."""
    raw = (os.environ.get("ENGRAMA_REMEMBER_DEDUPE") or "warn").strip().lower()
    return raw if raw in _DEDUPE_MODES else "warn"


def name_fragments(name: str) -> list[str]:
    """Substrings a candidate's lowercased name must contain one of.

    Each token of the normalised name contributes its first three characters
    (the whole token when shorter), so a typo or an accent later in a word
    still finds the candidate. Stores apply them as ``CONTAINS`` / ``LIKE``
    filters, rank exact names and similar lengths first, and cap the result.
    """
    tokens = normalise_key(name).split()
    frags = {t[:3] for t in tokens if len(t) >= 3} or set(tokens)
    return sorted(frags)


def similarity(a: str, b: str) -> float:
    """``difflib`` ratio on normalised names (1.0 when they normalise equal)."""
    na, nb = normalise_key(a), normalise_key(b)
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass(frozen=True)
class TargetDecision:
    """What to do with an inline relation target.

    ``kind`` is ``"connect"`` (use ``label``/``name``), ``"ask"`` (report
    ``candidates`` and write nothing; ``reason`` says why) or ``"create"``
    (mint a stub).
    """

    kind: str
    label: str | None = None
    name: str | None = None
    score: float = 1.0
    fuzzy: bool = False
    reason: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)


def _cand(c: Mapping[str, Any], score: float) -> dict[str, Any]:
    out = {"name": c["name"], "label": c["label"], "score": round(score, 3)}
    if c.get("status") == "archived":
        out["status"] = "archived"
    return out


def decide_target(
    target: str, explicit_label: str | None, candidates: Iterable[Mapping[str, Any]]
) -> TargetDecision:
    """Resolve one inline relation target against in-scope ``candidates``.

    ``candidates`` are ``{label, name, status}`` from ``name_candidates``.
    """
    scored = sorted(
        ((similarity(target, c["name"]), c) for c in candidates),
        key=lambda t: (-t[0], t[1]["label"], t[1]["name"]),
    )
    exact = [c for s, c in scored if s == 1.0]
    live_exact = [c for c in exact if c.get("status") != "archived"]

    if exact:
        if explicit_label:
            same = [c for c in live_exact if c["label"] == explicit_label]
            if same:
                return TargetDecision("connect", same[0]["label"], same[0]["name"])
            if live_exact:
                return TargetDecision(
                    "ask", reason="label_conflict", candidates=[_cand(c, 1.0) for c in live_exact]
                )
        elif len(live_exact) == 1:
            return TargetDecision("connect", live_exact[0]["label"], live_exact[0]["name"])
        elif len(live_exact) > 1:
            return TargetDecision(
                "ask", reason="label_conflict", candidates=[_cand(c, 1.0) for c in live_exact]
            )
        archived = [c for c in exact if c.get("status") == "archived"]
        if archived and not live_exact:
            return TargetDecision(
                "ask", reason="revive_candidate", candidates=[_cand(c, 1.0) for c in archived]
            )

    live = [(s, c) for s, c in scored if s < 1.0 and c.get("status") != "archived"]
    if explicit_label:
        live = [(s, c) for s, c in live if c["label"] == explicit_label] or live
    suggest = [(s, c) for s, c in live if s >= suggest_ratio()]
    if not suggest:
        return TargetDecision("create")
    top_score, top = suggest[0]
    runner_up = suggest[1][0] if len(suggest) > 1 else 0.0
    if top_score >= connect_ratio() and top_score - runner_up >= CONNECT_MARGIN:
        return TargetDecision("connect", top["label"], top["name"], score=top_score, fuzzy=True)
    return TargetDecision(
        "ask",
        reason="ambiguous",
        candidates=[_cand(c, s) for s, c in suggest[:SUGGEST_LIMIT]],
    )


def possible_duplicates(
    label: str,
    name: str,
    candidates: Iterable[Mapping[str, Any]],
    vector_hits: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Existing nodes a newly created ``label``/``name`` may duplicate.

    ``candidates`` come from ``name_candidates``; ``vector_hits`` are
    ``{label, name, cosine}`` from a kNN search with the new node's embedding.
    The node itself is excluded. Best reason per node, strongest first.
    """
    found: dict[tuple[str, str], dict[str, Any]] = {}

    def add(c: Mapping[str, Any], score: float, reason: str) -> None:
        key = (c["label"], c["name"])
        if key == (label, name):
            return
        if key not in found or score > found[key]["score"]:
            found[key] = {**_cand(c, score), "reason": reason}

    for c in candidates:
        s = similarity(name, c["name"])
        if s == 1.0 and c["label"] != label:
            add(c, s, "same name, different label")
        elif s >= duplicate_ratio():
            add(c, s, "similar name")
    for h in vector_hits:
        if h.get("cosine", 0.0) >= duplicate_cosine():
            add(h, h["cosine"], "similar meaning")
    return sorted(found.values(), key=lambda d: -d["score"])[:DUPLICATE_LIMIT]


__all__ = [
    "TargetDecision",
    "connect_ratio",
    "cosine",
    "decide_target",
    "dedupe_mode",
    "duplicate_cosine",
    "duplicate_ratio",
    "name_fragments",
    "possible_duplicates",
    "similarity",
    "suggest_ratio",
]
