"""
Engrama — Cypher identifier escaping for the Neo4j backend.

Node labels, relationship types and **property keys** cannot be passed as query
parameters in Cypher — they are structural and must be interpolated into the
query text. Labels and relationship types are whitelisted against the schema
enums before they reach the store, but property **key names** arrive in a
free-form ``properties`` bag (e.g. from the ``engrama_remember`` MCP tool) and
are *not* validated upstream. Interpolating such a name raw into
``SET n.<key> = $p0`` lets a malformed key (a backtick, a space, ``=``) break
the Cypher parser — the same class of defect as the unescaped Lucene query
(see :mod:`engrama.backends.neo4j._lucene`).

:func:`escape_cypher_identifier` backtick-quotes an identifier so any name is
matched literally and can never alter the query structure. This mirrors Neo4j's
own rule for quoting identifiers: wrap in backticks, and escape a literal
backtick by doubling it.
"""

from __future__ import annotations


def escape_cypher_identifier(name: str) -> str:
    """Return ``name`` as a safely backtick-quoted Cypher identifier.

    Any embedded backtick is doubled, then the whole token is wrapped in
    backticks — so ``weird key`` becomes ``` `weird key` ``` and ``a`b`` becomes
    ``` `a``b` ```. Safe to interpolate into Cypher in place of a raw label,
    relationship type or property key.
    """
    return "`" + name.replace("`", "``") + "`"
