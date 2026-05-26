"""Per-request scope resolver tests (Spec 001, T006 / FR-3, FR-7).

resolve_scope reads identity from the request headers; absent headers fall
back to the standalone single-user identity; a partial/malformed request is
fail-closed (ScopeUnresolved).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engrama.adapters.mcp.server import ScopeUnresolved, resolve_scope
from engrama.core.scope import MemoryScope


def _ctx(headers=None, *, has_request=True, standalone_sub="standalone-1", lifespan=None):
    request = SimpleNamespace(headers=headers or {}) if has_request else None
    if lifespan is None:
        lifespan = {"standalone_sub": standalone_sub}
    rc = SimpleNamespace(request=request, lifespan_context=lifespan)
    return SimpleNamespace(request_context=rc)


def test_both_headers_resolve_to_that_scope():
    ctx = _ctx({"x-engrama-org-id": "acme", "x-engrama-user-id": "alice"})
    assert resolve_scope(ctx) == MemoryScope(org_id="acme", user_id="alice")


def test_no_request_falls_back_to_standalone():
    ctx = _ctx(has_request=False, standalone_sub="sub-xyz")
    assert resolve_scope(ctx) == MemoryScope(org_id="sub-xyz", user_id="sub-xyz")


def test_absent_headers_fall_back_to_standalone():
    ctx = _ctx({}, standalone_sub="sub-xyz")
    assert resolve_scope(ctx) == MemoryScope(org_id="sub-xyz", user_id="sub-xyz")


def test_blank_header_values_treated_as_absent():
    ctx = _ctx({"x-engrama-org-id": "  ", "x-engrama-user-id": ""}, standalone_sub="sub-xyz")
    assert resolve_scope(ctx) == MemoryScope(org_id="sub-xyz", user_id="sub-xyz")


def test_org_only_is_unresolved():
    ctx = _ctx({"x-engrama-org-id": "acme"})
    with pytest.raises(ScopeUnresolved):
        resolve_scope(ctx)


def test_user_only_is_unresolved():
    ctx = _ctx({"x-engrama-user-id": "alice"})
    with pytest.raises(ScopeUnresolved):
        resolve_scope(ctx)


def test_fallback_to_local_sub_when_no_lifespan_value(monkeypatch):
    monkeypatch.setenv("ENGRAMA_LOCAL_SUB", "envsub")
    ctx = _ctx(has_request=False, lifespan={})
    assert resolve_scope(ctx) == MemoryScope(org_id="envsub", user_id="envsub")
