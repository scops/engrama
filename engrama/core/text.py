"""
Engrama — Text representation for embedding (re-export).

Canonical location: :mod:`engrama.embeddings.text`.
This module re-exports :func:`node_to_text` so callers can import from
either ``engrama.core.text`` or ``engrama.embeddings.text``.
"""

from engrama.embeddings.text import node_to_text

__all__ = ["node_to_text"]
