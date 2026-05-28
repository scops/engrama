"""Spec 001 T014 — pytest counterpart of ``scripts/check_scoped_queries.py``.

Runs the same AST scan as the CI guard but in-process, so a developer
running ``pytest`` locally catches new unscoped backend reads without
having to remember to invoke the script separately. T015 wires the
script itself into ``ci.yml`` as a warn-only step.

The scanner classifies a query-shaped string literal as a violation
when its enclosing function:

* does NOT route through the scope helper
  (:func:`engrama.core.scope.scope_filter_cypher` /
  :func:`engrama.core.scope.scope_filter_sql`), AND
* is NOT in the documented auto-exempt write/admin families, AND
* carries no inline ``# scope-exempt: <reason>`` comment.

A new function that adds a ``MATCH (`` or ``FROM nodes`` literal
without one of those three opt-ins will turn this test red.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def _scanner(_repo_root: Path):
    """Import ``scripts/check_scoped_queries`` once per module."""
    scripts_dir = _repo_root / "scripts"
    sys.path.insert(0, str(scripts_dir))
    try:
        import check_scoped_queries  # type: ignore[import-not-found]

        return check_scoped_queries
    finally:
        # Leave the path in place for the duration of the test session —
        # tearing it down between tests would re-import on every run.
        pass


def test_no_unscoped_backend_queries(_scanner, _repo_root: Path) -> None:
    """Every backend query is either scope-filtered, auto-exempt by
    function family, or carries an inline ``# scope-exempt`` comment.
    """
    violations = _scanner.scan(_repo_root)
    if violations:
        report = "\n".join(v.format() for v in violations)
        pytest.fail(
            f"{len(violations)} unscoped backend queries found:\n{report}\n\n"
            "Each one must route through scope_filter_cypher / scope_filter_sql, "
            "live in an auto-exempt write/admin family, or carry an inline "
            "'# scope-exempt: <reason>' comment. See "
            "scripts/check_scoped_queries.py for the rules."
        )


def test_scanner_recognises_helper_routing(_scanner, tmp_path: Path) -> None:
    """Sanity check: a synthetic function that calls scope_filter_sql is
    not reported as a violation (the helper-routing branch works).
    """
    src = tmp_path / "fake_backend.py"
    src.write_text(
        "def read(scope):\n"
        "    clause, params = scope_filter_sql(scope, 'nodes', json_column='props')\n"
        "    return f'SELECT id FROM nodes WHERE {clause}', params\n",
        encoding="utf-8",
    )
    assert _scanner._scan_file(src) == []


def test_scanner_recognises_inline_exempt_comment(_scanner, tmp_path: Path) -> None:
    """A function with an inline ``# scope-exempt:`` comment is not
    reported as a violation.
    """
    src = tmp_path / "fake_backend.py"
    src.write_text(
        "def read():\n"
        "    # scope-exempt: synthetic test fixture; not a real read path.\n"
        "    return 'SELECT id FROM nodes'\n",
        encoding="utf-8",
    )
    assert _scanner._scan_file(src) == []


def test_scanner_flags_an_unprotected_function(_scanner, tmp_path: Path) -> None:
    """A function with NO opt-in must be reported."""
    src = tmp_path / "fake_backend.py"
    src.write_text(
        "def leaky_read():\n    return 'SELECT id FROM nodes WHERE label = ?'\n",
        encoding="utf-8",
    )
    violations = _scanner._scan_file(src)
    assert len(violations) == 1
    assert violations[0].function == "leaky_read"


def test_scanner_recognises_auto_exempt_family(_scanner, tmp_path: Path) -> None:
    """A function whose name matches an auto-exempt write/admin family is
    not reported even without an inline comment — the rationale lives in
    the script's :data:`_AUTO_EXEMPT_PREFIXES` / :data:`_AUTO_EXEMPT_EXACT`.
    """
    src = tmp_path / "fake_backend.py"
    src.write_text(
        "def merge_thing():\n    return 'SELECT id FROM nodes WHERE label = ?'\n",
        encoding="utf-8",
    )
    assert _scanner._scan_file(src) == []
