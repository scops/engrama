"""
Engrama — Embedding vector health checks.

A vector is *degenerate* when it cannot serve as a meaningful key for
similarity search. Storing such a vector pollutes the hybrid-search
ranking: cosine similarity against an all-zero vector is either
undefined (divide-by-zero) or trivially uniform, so every query
returns the polluted node with a near-perfect score against every
unrelated topic.

This module centralises the detection criterion so the engine, the
reindex pipeline, and the contract tests all agree on what counts as
"unusable" — see issue #18.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# L2 norms below this threshold are considered effectively zero. Any
# real-world embedding (Ollama nomic-embed-text, OpenAI
# text-embedding-3-small, …) produces vectors with norm ≈ 1.0 after
# normalization, so a 1e-9 cutoff comfortably separates "all zeros" /
# "single-bit float noise" from any genuine output.
_DEGENERATE_NORM_EPSILON: float = 1e-9


def is_degenerate_vector(vector: Sequence[float] | None) -> bool:
    """Return ``True`` if ``vector`` is unusable for similarity search.

    A vector is flagged as degenerate when:

    - it is ``None`` or empty (no embedding returned by the provider),
    - its L2 norm is effectively zero — i.e. ``sum(x_i**2) < epsilon``.

    Uniform but non-zero vectors (e.g. ``[0.5, 0.5, 0.5, 0.5]``) are
    deliberately *not* flagged here. Some legitimate embedders return
    near-uniform vectors for very short / out-of-vocabulary input, and
    a permanent quarantine for them would hide real content. The
    detection stays narrow: "we got something we definitely cannot
    rank against".

    Args:
        vector: The embedding returned by an ``EmbeddingProvider``.

    Returns:
        ``True`` iff the vector should not be persisted as the node's
        embedding and the node should be flagged for a later reindex.
    """
    if vector is None:
        return True
    if len(vector) == 0:
        return True
    norm_sq = 0.0
    for x in vector:
        fx = float(x)
        norm_sq += fx * fx
    return math.sqrt(norm_sq) < _DEGENERATE_NORM_EPSILON
