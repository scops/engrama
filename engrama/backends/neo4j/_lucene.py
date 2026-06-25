"""
Engrama — Lucene query-string sanitisation for the Neo4j fulltext index.

``db.index.fulltext.queryNodes`` parses its second argument with the Lucene
classic query parser **before** the analyzer tokenises it. That means a raw
user query containing Lucene syntax characters is interpreted as query syntax,
not as literal text — and a malformed fragment raises
``Neo.ClientError.Procedure.ProcedureCallFailed`` (a Lucene ``TokenMgrError``)
instead of returning results.

The trigger seen in production was a query like
``"CI/CD pipeline git repos ..."``: Lucene treats ``/`` as the start of a regex
literal, so ``/CD pipeline ..."`` is an unterminated regex and the parser dies
with a lexical error at the closing ``<EOF>``.

We never want callers to write Lucene query syntax — engrama search takes a
natural-language / keyword string — so the fix is to escape every special
character so it is matched literally. :func:`escape_lucene_query` mirrors the
canonical ``org.apache.lucene.queryparser.classic.QueryParserBase.escape``.
"""

from __future__ import annotations

# The canonical set of characters that are special to the Lucene classic query
# parser. Kept in sync with QueryParserBase.escape() — note ``&`` and ``|`` are
# escaped individually so the ``&&`` / ``||`` boolean operators cannot form,
# and ``/`` is included (the regex delimiter that caused the production crash).
_LUCENE_SPECIAL: frozenset[str] = frozenset(
    '\\+-!(){}[]^"~*?:/&|'
)


def escape_lucene_query(query: str) -> str:
    """Escape Lucene query-syntax characters so ``query`` matches literally.

    Each special character is prefixed with a backslash. The result is safe to
    pass as the ``$query`` parameter of ``db.index.fulltext.queryNodes`` — the
    parser sees only literal terms, then the index analyzer tokenises them as
    usual (so ``CI/CD`` still tokenises into ``ci`` + ``cd`` for matching).

    Empty / whitespace-only input is returned unchanged.
    """
    if not query:
        return query
    out: list[str] = []
    for ch in query:
        if ch in _LUCENE_SPECIAL:
            out.append("\\")
        out.append(ch)
    return "".join(out)
