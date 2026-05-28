"""Spec 001 T009a — scope in logging/trace context.

Every log line emitted during a tool call must carry the hashed
``(org_id, user_id)`` of the resolving request, so an aggregator can
group lines by tenant without the tool body having to thread the scope
through every ``logger.info`` call.

The binding is done via :class:`contextvars.ContextVar`, which is
copied per asyncio task — concurrent requests cannot bleed scopes into
each other's logs.
"""

from __future__ import annotations

import hashlib
import logging

import pytest

from engrama.adapters.mcp.server import (
    _LOG_SCOPE,
    _bind_scope_to_logging,
    _hash_id,
    _ScopeLogFilter,
)
from engrama.core.scope import MemoryScope


@pytest.fixture(autouse=True)
def _reset_log_scope():
    """Clear the contextvar between tests so leakage in one test doesn't
    pollute the next.
    """
    token = _LOG_SCOPE.set(None)
    yield
    _LOG_SCOPE.reset(token)


def _expected(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def test_hash_id_truncates_and_is_stable() -> None:
    assert _hash_id("acme-123") == _expected("acme-123")
    assert _hash_id("") == "-"
    assert _hash_id(None) == "-"
    # Different inputs hash to different prefixes (collision check on a
    # tiny sample is a sanity test, not a guarantee).
    assert _hash_id("acme-123") != _hash_id("globex-456")


def test_bind_scope_populates_contextvar() -> None:
    scope = MemoryScope(org_id="acme", user_id="alice")
    _bind_scope_to_logging(scope)
    bound = _LOG_SCOPE.get()
    assert bound == (_expected("acme"), _expected("alice"))


def test_log_filter_injects_bound_scope_onto_record() -> None:
    """A log line emitted while a scope is bound carries the hashed
    org/user on the record so a format string can pick them up.
    """
    scope = MemoryScope(org_id="acme", user_id="alice")
    _bind_scope_to_logging(scope)

    record = logging.LogRecord(
        name="engrama_mcp",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="testing",
        args=None,
        exc_info=None,
    )
    assert _ScopeLogFilter().filter(record) is True
    assert record.scope_org == _expected("acme")
    assert record.scope_user == _expected("alice")


def test_log_filter_emits_dash_when_no_scope_bound() -> None:
    """A log line emitted with no scope on the context (e.g. server
    startup, before any request) still passes the filter and carries
    placeholder ``-`` values rather than dropping or raising.
    """
    record = logging.LogRecord(
        name="engrama_mcp",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="startup line",
        args=None,
        exc_info=None,
    )
    assert _ScopeLogFilter().filter(record) is True
    assert record.scope_org == "-"
    assert record.scope_user == "-"


@pytest.mark.asyncio
async def test_scope_isolated_per_async_task() -> None:
    """Two concurrent tasks bind different scopes — each task's log
    filter sees only its own scope (``contextvars`` copies the context
    on task creation).
    """
    import asyncio

    captured: dict[str, tuple[str, str] | None] = {}

    async def _bind_and_observe(name: str, scope: MemoryScope) -> None:
        _bind_scope_to_logging(scope)
        # Yield control so the other task can run between bind and read;
        # if the var leaked across tasks, the value would change.
        await asyncio.sleep(0)
        captured[name] = _LOG_SCOPE.get()

    a = MemoryScope(org_id="orgA", user_id="userA")
    b = MemoryScope(org_id="orgB", user_id="userB")
    await asyncio.gather(_bind_and_observe("a", a), _bind_and_observe("b", b))

    assert captured["a"] == (_expected("orgA"), _expected("userA"))
    assert captured["b"] == (_expected("orgB"), _expected("userB"))
