"""The Neo4j runtime schema must ship inside the package and be safe to
apply on every connect.

#1 — when Engrama is installed as a dependency (e.g. a headless/SaaS pod
with no repo checkout), ``scripts/init-schema.cypher`` is not present, so
the fulltext index was never created and ``engrama_search`` crashed. The
fix packages ``engrama/backends/neo4j/schema.cypher`` and applies it on
connect. These tests validate the packaged artifact without needing the
``neo4j`` extra installed (so they run in the base CI matrix).
"""

from __future__ import annotations

from pathlib import Path

import engrama

# Resolve the packaged file via the top-level package so this test does not
# import the neo4j backend module (and thus does not require the optional
# ``neo4j`` driver to be installed).
_SCHEMA = Path(engrama.__file__).parent / "backends" / "neo4j" / "schema.cypher"


def _statements() -> list[str]:
    text = _SCHEMA.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))
    return [s.strip() for s in body.split(";") if s.strip()]


def test_schema_cypher_is_packaged() -> None:
    assert _SCHEMA.is_file(), "schema.cypher must be co-located in the neo4j backend package"


def test_schema_is_idempotent_and_non_destructive() -> None:
    """Applied on every connect against a possibly-populated graph, so it
    must never DROP/rebuild and must guard every statement with IF NOT
    EXISTS. SHOW (interactive verification) has no place in DDL applied
    programmatically."""
    stmts = _statements()
    assert stmts, "schema.cypher parsed to zero statements"
    for s in stmts:
        upper = s.upper()
        assert "DROP" not in upper, f"destructive DROP in runtime schema: {s[:60]}"
        assert "SHOW" not in upper, f"interactive SHOW in runtime schema: {s[:60]}"
        assert "IF NOT EXISTS" in upper, f"non-idempotent statement: {s[:60]}"


def test_fulltext_index_covers_origin_and_source() -> None:
    """The memory_search fulltext index must exist and index both the
    system ``source`` bucket and the caller-set semantic ``origin`` (#5)."""
    fulltext = [s for s in _statements() if "FULLTEXT INDEX memory_search" in s]
    assert len(fulltext) == 1, "exactly one memory_search fulltext index expected"
    stmt = fulltext[0]
    assert "n.origin" in stmt
    assert "n.source" in stmt
