#!/usr/bin/env python3
"""Spec 001 T014 — fail-closed scope CI guard.

Scans every backend module for "query-shaped" string literals (raw
Cypher ``MATCH (`` or SQLite ``FROM nodes`` / ``FROM edges``) and asserts
that the containing function either:

1. routes the query through the scoped helper (calls
   :func:`engrama.core.scope.scope_filter_cypher` /
   :func:`engrama.core.scope.scope_filter_sql`), **or**
2. carries an immediately preceding ``# scope-exempt: <reason>`` comment
   explicitly opting that function into the admin / migration / status
   path.

Functions that pass neither check are reported as violations and the
script exits non-zero.

The CI workflow (``T015``) runs this as **warn-only** today: the guard
is intentionally noisy while the migration is mid-flight, and is
flipped to blocking at T026 once US-2 is fully shippable. The companion
test ``tests/test_no_unscoped_match.py`` exercises this script in-
process so a developer running ``pytest`` locally gets the same signal
without having to remember to invoke the script.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# Backends are the primary surface that holds raw queries. Add new
# directories here if the project grows another backend.
_BACKEND_DIRS = (
    "engrama/backends/neo4j",
    "engrama/backends/sqlite",
)


# A query is "scoped" if the helper is referenced anywhere in the
# function body. We intentionally match the call site syntactically
# rather than dynamically — the linter cannot prove that a runtime call
# would actually splice the filter, but in practice every internal use
# follows the convention of binding the result to a local and then
# concatenating into the query string.
_SCOPE_HELPERS = ("scope_filter_cypher", "scope_filter_sql", "_scope_and")

# Function families whose contract is "write or admin operation, identity
# is the caller's responsibility upstream":
#
# - ``merge_*`` / ``delete_*`` / ``store_*`` / ``mark_*`` / ``update_*`` /
#   ``expire_*`` / ``archive_*`` / ``purge_*`` — writes that the engine's
#   fail-closed guard has already gated. A SELECT inside a write that
#   resolves the row to update doesn't need its own scope filter.
# - ``decay_*`` / ``query_at_date`` — admin temporal maintenance
#   (`engrama decay` / `engrama decay --dry-run`).
# - ``iter_all_*`` — migration / export.
# - ``health_check`` — runtime introspection (`engrama status`).
# - ``seed_*`` — first-run schema seeding.
# - ``find_obsidian_path`` / ``list_documented_nodes`` /
#   ``list_nodes_for_embedding`` / ``merge_wiki_link*`` — obsidian-sync
#   internal helpers; the vault is single-tenant per deployment today.
# - ``_sync_fts`` — internal FTS mirror invoked as a side-effect of
#   ``merge_node`` (already scoped at the engine).
#
# New functions outside these families that touch ``nodes`` / ``edges``
# without going through the scope helper MUST add an inline
# ``# scope-exempt: <reason>`` comment to opt in explicitly.
_AUTO_EXEMPT_PREFIXES = (
    "merge_",
    "delete_",
    "store_",
    "mark_",
    "update_",
    "expire_",
    "archive_",
    "purge_",
    "decay_",
    "iter_all_",
    "seed_",
    "_sync_",
)
_AUTO_EXEMPT_EXACT = frozenset(
    {
        "get_node",
        "health_check",
        "query_at_date",
        "find_obsidian_path",
        "list_documented_nodes",
        "list_nodes_for_embedding",
    }
)


def _is_auto_exempt(function_name: str) -> bool:
    """``True`` for functions whose family is documented as exempt above."""
    if function_name in _AUTO_EXEMPT_EXACT:
        return True
    return any(function_name.startswith(p) for p in _AUTO_EXEMPT_PREFIXES)


# Markers that classify a string literal as "query-shaped" for our
# purposes. Plain ``MATCH (`` is the Cypher fence; ``FROM nodes`` /
# ``FROM edges`` capture SQLite reads (we don't try to detect plain
# ``SELECT`` because every parameterised SQL fragment contains one).
_CYPHER_MARKERS = ("MATCH (",)
_SQLITE_MARKERS = ("FROM nodes", "FROM edges")
_ALL_MARKERS = _CYPHER_MARKERS + _SQLITE_MARKERS


@dataclass(frozen=True)
class _Violation:
    path: Path
    function: str
    lineno: int
    marker: str
    snippet: str

    def format(self) -> str:
        return (
            f"{self.path}:{self.lineno}: function {self.function!r} contains "
            f"a {self.marker!r} query but does not route through "
            f"scope_filter_* and carries no '# scope-exempt:' comment.\n"
            f"  query snippet: {self.snippet[:120]!r}"
        )


def _string_literals(node: ast.AST) -> list[ast.Constant]:
    """Yield every string :class:`ast.Constant` reachable from *node*."""
    out: list[ast.Constant] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            out.append(child)
    return out


def _function_calls(node: ast.AST) -> set[str]:
    """Return the set of plain function names called inside *node*.

    Catches ``scope_filter_cypher(...)``, ``self._scope_and(...)``,
    ``scope_filter_sql(...)`` etc. by recording the trailing attribute
    on attribute calls too.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _function_source(path: Path, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Return the raw source text of *fn* including its decorators and
    comments — used to scan for ``# scope-exempt`` markers.
    """
    text = path.read_text(encoding="utf-8").splitlines()
    start = fn.lineno - 1
    end = (fn.end_lineno or fn.lineno) - 1
    return "\n".join(text[start : end + 1])


def _scan_file(path: Path) -> list[_Violation]:
    """Walk *path*, returning a list of unscoped-query violations."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[_Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue

        # First, the cheapest signals: does the function call the helper
        # anywhere? Does it carry a scope-exempt comment? Both checks
        # apply to the whole function body, since string literals can be
        # built up across multiple statements.
        called = _function_calls(node)
        uses_helper = bool(called & set(_SCOPE_HELPERS))
        source = _function_source(path, node)
        is_exempt = "# scope-exempt:" in source
        auto_exempt = _is_auto_exempt(node.name)

        if uses_helper or is_exempt or auto_exempt:
            continue

        # Otherwise look for query-shaped string literals. If we find
        # any, the function is a violation.
        for lit in _string_literals(node):
            for marker in _ALL_MARKERS:
                if marker in lit.value:
                    violations.append(
                        _Violation(
                            path=path,
                            function=node.name,
                            lineno=lit.lineno,
                            marker=marker,
                            snippet=lit.value.strip(),
                        )
                    )
                    break  # one report per literal is enough
    return violations


def scan(repo_root: Path) -> list[_Violation]:
    """Scan every backend module under *repo_root* for unscoped queries."""
    violations: list[_Violation] = []
    for directory in _BACKEND_DIRS:
        base = repo_root / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.name == "__init__.py":
                continue
            violations.extend(_scan_file(path))
    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    violations = scan(repo_root)
    if not violations:
        print("scope-CI: 0 unscoped backend queries found (ok)")
        return 0
    print(f"scope-CI: {len(violations)} unscoped backend queries found:")
    for v in violations:
        print(v.format())
        print("---")
    print(
        "Each function MUST either route through scope_filter_cypher / "
        "scope_filter_sql, or carry an immediately-visible "
        "'# scope-exempt: <reason>' comment classifying it as admin / "
        "migration / status. See Spec 001 T014."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
