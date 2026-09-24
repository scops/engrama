"""Scope model for multi-scope memory (DDR-003 Phase F / Roadmap P14).

This module defines :class:`MemoryScope` — the four-dimension address
that locates a write inside the org → user → agent → session hierarchy
— and the helpers that translate it into SQL or Cypher WHERE fragments
on the read side.

Visibility rule (Spec 001, FR-2 — fail-closed): the isolation boundary is
the ``(org_id, user_id)`` pair only (research R-1; ``agent_id``/``session_id``
are provenance, never filtered). A node ``N`` is visible at a resolved scope
``S`` iff ``N.org_id == S.org_id`` **and** ``N.user_id IN (S.user_id,
"__entity__")``. There is no ``IS NULL OR`` inheritance: a node missing the
matching identity is simply not visible. ``"__entity__"`` is the org-shared
sentinel ``user_id`` — visible to every request carrying the same ``org_id``.

Hard fail-closed (Spec 001, FR-5): a scope that is ``None`` or is missing
either ``org_id`` or ``user_id`` is an **illegal state for a tenant read** —
it must never resolve to "see everything". The helpers return a match-nothing
clause (``(false)`` / ``(1 = 0)``) in that case, so a bug that lets an
incomplete scope reach a read yields **zero** rows rather than a leak. There
is no "unscoped = admin" path through these helpers; genuine cross-tenant
admin operations (export, migration backfill, GC of identity-less orphans)
use their own explicit, CI-allowlisted queries and never call these helpers.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Ordered for stable parameter generation / readable WHERE clauses.
_DIMENSIONS: tuple[str, ...] = ("org_id", "user_id", "agent_id", "session_id")

# The isolation boundary (Spec 001, R-1). agent_id/session_id are provenance
# only and are never used to filter reads.
_FILTER_DIMENSIONS: tuple[str, ...] = ("org_id", "user_id")

# Sentinel user_id for org-shared nodes: visible to every request carrying the
# same org_id (Spec 001, FR-8). A real identity may never equal this value.
ENTITY_SENTINEL: str = "__entity__"

# Operators set these to opt a deployment into multi-scope memory.
_ENV_VARS: dict[str, str] = {
    "org_id": "ENGRAMA_ORG_ID",
    "user_id": "ENGRAMA_USER_ID",
    "agent_id": "ENGRAMA_AGENT_ID",
    "session_id": "ENGRAMA_SESSION_ID",
}


class ScopeIncomplete(Exception):
    """Raised when a write reaches the engine with a missing or partial
    scope. The MCP boundary rejects such requests up front with
    :class:`engrama.adapters.mcp.server.ScopeUnresolved`; this
    engine-layer exception is defence-in-depth (Spec 001, T011): a direct
    SDK or skill bypass that forgets to set ``default_scope`` cannot
    silently persist an identity-less node.

    The exception carries the offending scope so callers can log it
    without re-deriving the failure context.
    """

    def __init__(self, message: str, scope: MemoryScope | None = None) -> None:
        super().__init__(message)
        self.scope = scope


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

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> MemoryScope:
        """Build a :class:`MemoryScope` from operator-set env vars.

        Reads ``ENGRAMA_ORG_ID``, ``ENGRAMA_USER_ID``, ``ENGRAMA_AGENT_ID``
        and ``ENGRAMA_SESSION_ID``. Unset variables stay ``None`` —
        a deployment with no env vars set produces an empty scope, the
        same as ``MemoryScope()``, which the engine then treats as a
        no-op (no writes get tagged, no reads get filtered).

        ``environ`` defaults to :data:`os.environ` but can be passed
        explicitly for tests that prefer not to mutate global state.
        """
        env = os.environ if environ is None else environ
        return cls(
            org_id=env.get(_ENV_VARS["org_id"]) or None,
            user_id=env.get(_ENV_VARS["user_id"]) or None,
            agent_id=env.get(_ENV_VARS["agent_id"]) or None,
            session_id=env.get(_ENV_VARS["session_id"]) or None,
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
    # Hard fail-closed: a read without a complete (org_id, user_id) is a bug,
    # never "see all" — match nothing.
    if scope is None or not scope.org_id or not scope.user_id:
        return "(1 = 0)", {}
    if json_column:
        org_col = f"json_extract({table_alias}.{json_column}, '$.org_id')"
        user_col = f"json_extract({table_alias}.{json_column}, '$.user_id')"
    else:
        org_col = f"{table_alias}.org_id"
        user_col = f"{table_alias}.user_id"
    clause = f"({org_col} = :scope_org_id AND {user_col} IN (:scope_user_id, :scope_entity))"
    params: dict[str, Any] = {
        "scope_org_id": scope.org_id,
        "scope_user_id": scope.user_id,
        "scope_entity": ENTITY_SENTINEL,
    }
    return clause, params


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
    # Hard fail-closed: a read without a complete (org_id, user_id) is a bug,
    # never "see all" — match nothing.
    if scope is None or not scope.org_id or not scope.user_id:
        return "(false)", {}
    clause = (
        f"({node_var}.org_id = $scope_org_id "
        f"AND {node_var}.user_id IN [$scope_user_id, $scope_entity])"
    )
    params: dict[str, Any] = {
        "scope_org_id": scope.org_id,
        "scope_user_id": scope.user_id,
        "scope_entity": ENTITY_SENTINEL,
    }
    return clause, params


def owner_filter_sql(
    owner: MemoryScope | None,
    table_alias: str,
    *,
    json_column: str = "props",
) -> tuple[str, dict[str, Any]]:
    """Build a SQLite WHERE fragment matching nodes owned by exactly ``owner``.

    The write-side counterpart of :func:`scope_filter_sql`: a key-addressed
    write (merge, re-embed) targets the writer's *own* node, not an
    org-shared ``__entity__`` node or another owner's same-named node. The
    expressions mirror the ``idx_nodes_identity`` unique index, so ``owner``
    ``None`` (identity-less) matches ``('', '')`` exactly as the index stores it.
    """
    _check_identifier(table_alias, "table_alias")
    _check_identifier(json_column, "json_column")
    col = f"{table_alias}.{json_column}"
    clause = (
        f"(COALESCE(json_extract({col}, '$.org_id'), '') = :owner_org_id "
        f"AND COALESCE(json_extract({col}, '$.user_id'), '') = :owner_user_id)"
    )
    if owner is None:
        return clause, {"owner_org_id": "", "owner_user_id": ""}
    return clause, {"owner_org_id": owner.org_id or "", "owner_user_id": owner.user_id or ""}


def owner_filter_cypher(owner: MemoryScope | None, node_var: str) -> tuple[str, dict[str, Any]]:
    """Cypher counterpart of :func:`owner_filter_sql` (``$name`` placeholders).

    A missing dimension (``owner`` ``None``, or a partial owner) must be
    absent on the node too, mirroring the SQLite ``''`` bucket.
    """
    _check_identifier(node_var, "node_var")
    parts: list[str] = []
    params: dict[str, Any] = {}
    for dim in _FILTER_DIMENSIONS:
        value = getattr(owner, dim) if owner is not None else None
        if value:
            parts.append(f"{node_var}.{dim} = $owner_{dim}")
            params[f"owner_{dim}"] = value
        else:
            parts.append(f"{node_var}.{dim} IS NULL")
    return "(" + " AND ".join(parts) + ")", params


def node_owner(properties: Mapping[str, Any]) -> MemoryScope | None:
    """Return the owning ``(org_id, user_id)`` carried by a node's properties.

    A node's owner is part of its identity: the merge key is
    ``(label, name|title, org_id, user_id)``, so two owners writing the same
    name get two nodes. Key-addressed writes use this to target exactly the
    writer's own node.

    Both dimensions absent → ``None``: an identity-less (legacy / admin) node.
    A partial owner (only one dimension, e.g. a pre-Spec-001 row) is returned
    as-is. Matching is exact per dimension, so neither form can ever reach a
    node owned by a complete identity; the engine and MCP boundaries reject
    such writes before they get here anyway.
    """
    org_id = properties.get("org_id") or None
    user_id = properties.get("user_id") or None
    if org_id is None and user_id is None:
        return None
    return MemoryScope(org_id=org_id, user_id=user_id)


def node_visible(scope: MemoryScope | None, org_id: Any, user_id: Any) -> bool:
    """Return ``True`` iff a node with the given provenance is visible at ``scope``.

    The in-Python counterpart of :func:`scope_filter_sql` /
    :func:`scope_filter_cypher`, for the rare path that has already loaded a node
    and must check it against a scope (e.g. the SQLite root-node lookup in
    ``get_node_with_neighbours``).

    **Fail-closed**, with the *same* rule as the SQL/Cypher filters: a ``None`` or
    incomplete scope sees nothing. A node is visible only to a request carrying
    the same ``org_id`` and either the node's own ``user_id`` or the org-shared
    ``__entity__`` sentinel.
    """
    if scope is None or not scope.org_id or not scope.user_id:
        return False
    return org_id == scope.org_id and user_id in (scope.user_id, ENTITY_SENTINEL)


__all__ = [
    "ENTITY_SENTINEL",
    "MemoryScope",
    "ScopeIncomplete",
    "node_owner",
    "node_visible",
    "owner_filter_cypher",
    "owner_filter_sql",
    "scope_filter_cypher",
    "scope_filter_sql",
]
