"""Name normalisation shared by every comparison of node names."""

from __future__ import annotations

import unicodedata


def normalise_key(value: str) -> str:
    """Casefold, strip accents, and unify ``-``/``_``/whitespace runs."""
    decomposed = unicodedata.normalize("NFKD", value)
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    spaced = no_accents.casefold().replace("-", " ").replace("_", " ")
    return " ".join(spaced.split())


__all__ = ["normalise_key"]
