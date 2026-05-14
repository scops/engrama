"""Scope model for multi-scope memory (DDR-003 Phase F / Roadmap P14).

This module defines :class:`MemoryScope` — the four-dimension address
that locates a write inside the org → user → agent → session hierarchy
— and the helpers that translate it into SQL or Cypher WHERE fragments
on the read side.

Visibility rule (PR-F2): a node ``N`` is visible at scope ``S`` iff for
each dimension ``d`` where ``S.d`` is set, ``N.d IS NULL OR N.d == S.d``.
Dimensions that are ``None`` on ``S`` are not filtered — they act as a
wildcard. This implements DDR-003 Part 6's "broader-scope inheritance":
a query at ``user_id="alice"`` sees her writes, the matching org-level
writes (no ``user_id``), and the truly global writes (no scope at all).

Per DDR-003 Part 6, for v1 Engrama stays single-user: every dimension
defaults to ``None``, ``MemoryScope().to_properties()`` is empty, and
the node carries no scope properties. Switching to multi-user is an
operator decision (set the dimensions when constructing the engine or
the SDK), not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Ordered for stable parameter generation / readable WHERE clauses.
_DIMENSIONS: tuple[str, ...] = ("org_id", "user_id", "agent_id", "session_id")


@dataclass(frozen=True)
class MemoryScope:
    """The four-dimensional scope of a memory operation.

    Hierarchy from broadest to narrowest::

        org_id (broadest)
          └── user_id
                └── agent_id
                      └── session_id (narrowest)

    Dimensions left as ``None`` are unscoped — for v1 single-user
    deployments every field defaults to ``None``, which is equivalent to
    "no scope" and means writes carry no scope properties and reads
    apply no scope filter.

    Frozen so callers can't mutate a scope after handing it off across
    layers.
    """

    org_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None

    def to_properties(self) -> dict[str, Any]:
        """Return the non-``None`` dimensions as a flat property dict.

        Empty dict when every dimension is ``None`` — the engine then
        adds nothing to the node, preserving the single-user default.
        """
        out: dict[str, Any] = {}
        if self.org_id is not None:
            out["org_id"] = self.org_id
        if self.user_id is not None:
            out["user_id"] = self.user_id
        if self.agent_id is not None:
            out["agent_id"] = self.agent_id
        if self.session_id is not None:
            out["session_id"] = self.session_id
        return out

    def is_empty(self) -> bool:
        """``True`` iff every dimension is ``None`` (no-op scope)."""
        return (
            self.org_id is None
            and self.user_id is None
            and self.agent_id is None
            and self.session_id is None
        )


def _check_identifier(name: str, kind: str) -> None:
    """Reject ``name`` if it isn't a valid Python identifier.

    The scope-filter helpers interpolate identifiers directly into SQL
    / Cypher strings — values come from internal call sites today (``"n"``,
    ``"node"``, ``"props"``) but the function signatures accept ``str``,
    so a future caller could pass tainted data. ``str.isidentifier()`` is
    the cheap fence that keeps the helpers safe from injection if that
    happens: it allows ``[A-Za-z_][A-Za-z0-9_]*`` only, which is the
    intersection of valid SQL identifiers (when unquoted), Cypher labels,
    and Python names.
    """
    if not name or not name.isidentifier():
        raise ValueError(f"{kind} must be a valid identifier, got {name!r}")


def scope_filter_sql(
    scope: MemoryScope | None,
    table_alias: str,
    *,
    json_column: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build a SQLite WHERE fragment + named params for a scope.

    Returns ``("", {})`` when ``scope`` is ``None`` or empty — callers
    can use the result unconditionally and concatenate when truthy. The
    fragment uses ``:name`` named-parameter placeholders.

    When ``json_column`` is set, dimensions are read via
    ``json_extract({table_alias}.{json_column}, '$.{dim}')`` instead of
    plain ``{table_alias}.{dim}`` — needed for the SQLite backend, which
    stores all node properties inside a single JSON ``props`` column.

    ``table_alias`` and ``json_column`` are validated as Python
    identifiers before being interpolated into the SQL string.

    Example::

        clause, params = scope_filter_sql(scope, "n", json_column="props")
        if clause:
            sql += f" AND {clause}"
            cursor.execute(sql, {**other_params, **params})
    """
    _check_identifier(table_alias, "table_alias")
    if json_column is not None:
        _check_identifier(json_column, "json_column")
    if scope is None or scope.is_empty():
        return "", {}
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for dim in _DIMENSIONS:
        value = getattr(scope, dim)
        if value is None:
            continue
        param_name = f"scope_{dim}"
        if json_column:
            col_expr = f"json_extract({table_alias}.{json_column}, '$.{dim}')"
        else:
            col_expr = f"{table_alias}.{dim}"
        clauses.append(f"({col_expr} IS NULL OR {col_expr} = :{param_name})")
        params[param_name] = value
    return " AND ".join(clauses), params


def scope_filter_cypher(
    scope: MemoryScope | None,
    node_var: str,
) -> tuple[str, dict[str, Any]]:
    """Build a Cypher WHERE fragment + params for a scope.

    Same semantics as :func:`scope_filter_sql`, but with Cypher
    ``$name`` placeholders. Returns ``("", {})`` for ``None`` or empty
    scopes so the call site can unconditionally concat. ``node_var`` is
    validated as a Python identifier before interpolation.
    """
    _check_identifier(node_var, "node_var")
    if scope is None or scope.is_empty():
        return "", {}
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for dim in _DIMENSIONS:
        value = getattr(scope, dim)
        if value is None:
            continue
        param_name = f"scope_{dim}"
        clauses.append(f"({node_var}.{dim} IS NULL OR {node_var}.{dim} = ${param_name})")
        params[param_name] = value
    return " AND ".join(clauses), params


__all__ = ["MemoryScope", "scope_filter_cypher", "scope_filter_sql"]
