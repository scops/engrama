"""
Engrama — unit tests for Cypher identifier (property-key) escaping.

Hardening for the Cypher-injection surface where a caller-supplied property key
(e.g. via the ``engrama_remember`` ``properties`` bag) is interpolated into
``SET n.<key> = $p0``. An unquoted key containing a backtick, space or ``=``
breaks the parser — the same class of defect as the unescaped Lucene query.

Pure-function tests — no Neo4j required.
"""

from __future__ import annotations

import pytest

# Helper lives in the neo4j backend package whose ``__init__`` imports the
# driver, so gate on the same extra as the rest of the backend suite.
pytest.importorskip("neo4j")

from engrama.backends.neo4j._cypher import escape_cypher_identifier  # noqa: E402


def test_plain_key_is_backtick_wrapped() -> None:
    assert escape_cypher_identifier("name") == "`name`"


def test_key_with_space_is_safe() -> None:
    assert escape_cypher_identifier("weird key") == "`weird key`"


def test_embedded_backtick_is_doubled() -> None:
    # The one character that can break out of a backtick-quoted identifier
    # must be doubled, not left bare.
    assert escape_cypher_identifier("a`b") == "`a``b`"


def test_injection_attempt_stays_inside_quotes() -> None:
    # A key crafted to break out of the SET clause is rendered as one literal
    # (harmless) identifier — no unbalanced backticks escape the quoting.
    escaped = escape_cypher_identifier("x = 1 WITH n MATCH (m) DETACH DELETE m //")
    assert escaped.startswith("`") and escaped.endswith("`")
    assert escaped.count("`") == 2  # only the wrapping pair
