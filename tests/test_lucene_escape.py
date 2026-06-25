"""
Engrama — unit tests for Lucene query-string escaping.

Regression for the production crash where a query containing ``/`` (e.g.
``"CI/CD pipeline ..."``) made ``db.index.fulltext.queryNodes`` raise a Lucene
``TokenMgrError`` (``Neo.ClientError.Procedure.ProcedureCallFailed``) because
the parser read ``/`` as the start of an unterminated regex literal.

Pure-function tests — no Neo4j required.
"""

from __future__ import annotations

import pytest

# The helper itself has no third-party dependency, but it lives in the neo4j
# backend package whose ``__init__`` imports the driver — so this test runs
# wherever the ``neo4j`` extra is installed (the same gate as the rest of the
# neo4j backend suite).
pytest.importorskip("neo4j")

from engrama.backends.neo4j._lucene import escape_lucene_query  # noqa: E402


def test_escapes_slash_the_production_trigger() -> None:
    # The exact shape that crashed in SaaS: a forward slash mid-query.
    assert (
        escape_lucene_query("CI/CD pipeline git repos on-premise administracion entornos")
        == "CI\\/CD pipeline git repos on\\-premise administracion entornos"
    )


@pytest.mark.parametrize(
    "char",
    list('\\+-!(){}[]^"~*?:/&|'),
)
def test_every_special_char_is_backslash_escaped(char: str) -> None:
    assert escape_lucene_query(f"a{char}b") == f"a\\{char}b"


def test_plain_text_is_untouched() -> None:
    q = "simple keyword query without specials"
    assert escape_lucene_query(q) == q


def test_empty_string_passthrough() -> None:
    assert escape_lucene_query("") == ""


def test_boolean_operators_cannot_form() -> None:
    # ``&&`` / ``||`` must not survive as Lucene boolean operators.
    assert escape_lucene_query("a && b") == "a \\&\\& b"
    assert escape_lucene_query("a || b") == "a \\|\\| b"
