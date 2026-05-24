"""Tests for the Streamable HTTP transport layer of the Engrama MCP server.

These cover the transport-only surface added behind ``ENGRAMA_TRANSPORT=http``:
the stdio↔http switch in the entry point, the ``/health`` probe, the
DNS-rebinding Origin check (bad Origin → 403), and the RFC 9728
``/.well-known/oauth-protected-resource`` stub. They never open a real
backend — the store is mocked — so they are safe to run against the shared
local Neo4j.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from starlette.testclient import TestClient

from engrama.adapters.mcp.server import create_engrama_mcp

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal async store exposing just ``health_check`` / ``close``."""

    def __init__(self, *, healthy: bool) -> None:
        self._healthy = healthy
        self.closed = False

    async def health_check(self) -> dict[str, Any]:
        if not self._healthy:
            raise RuntimeError("backend unreachable")
        return {"status": "ok", "backend": "sqlite-async", "node_count": 7}

    async def close(self) -> None:
        self.closed = True


def _sqlite_mcp(**kwargs: Any):
    """A FastMCP instance pinned to the in-memory-ish sqlite config."""
    return create_engrama_mcp(
        backend="sqlite",
        config={"GRAPH_BACKEND": "sqlite"},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Transport switching (entry point)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transport_env", "expected"),
    [("http", "streamable-http"), ("stdio", "stdio")],
)
def test_main_selects_transport_from_env(
    monkeypatch: pytest.MonkeyPatch,
    transport_env: str,
    expected: str,
) -> None:
    """``ENGRAMA_TRANSPORT`` picks the FastMCP transport without touching stdio."""
    captured: dict[str, str] = {}

    def fake_run(self: Any, transport: str = "stdio", mount_path: Any = None) -> None:  # noqa: ARG001
        captured["transport"] = transport

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.run", fake_run)
    monkeypatch.setenv("ENGRAMA_TRANSPORT", transport_env)
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setattr(sys, "argv", ["engrama-mcp"])

    import engrama.adapters.mcp as adapter

    adapter.main()

    assert captured["transport"] == expected


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_200_when_backend_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "engrama.backends.create_async_stores",
        lambda cfg: (_FakeStore(healthy=True), None),
    )
    app = _sqlite_mcp().streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["backend"] == "sqlite-async"
    assert body["node_count"] == 7


def test_health_returns_503_when_backend_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "engrama.backends.create_async_stores",
        lambda cfg: (_FakeStore(healthy=False), None),
    )
    app = _sqlite_mcp().streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "error"


# ---------------------------------------------------------------------------
# Origin validation (DNS-rebinding protection)
# ---------------------------------------------------------------------------


def test_disallowed_origin_is_rejected_with_403() -> None:
    app = _sqlite_mcp(host="127.0.0.1", port=8000).streamable_http_app()
    # base_url drives the Host header → must match the allow-list, otherwise
    # the request fails the Host check (421) before reaching the Origin check.
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        resp = client.post(
            "/mcp",
            headers={
                "Origin": "http://evil.com",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert resp.status_code == 403


def test_allowed_origin_passes_security_check() -> None:
    app = _sqlite_mcp(host="127.0.0.1", port=8000).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        resp = client.post(
            "/mcp",
            headers={
                "Origin": "http://localhost:8000",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    # The Origin check passes (loopback wildcard); whatever the MCP layer
    # then does, it must not be a security rejection.
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# OAuth protected-resource stub (RFC 9728)
# ---------------------------------------------------------------------------


def test_oauth_metadata_returns_404_without_issuer() -> None:
    app = _sqlite_mcp(auth_issuer=None).streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 404


def test_oauth_metadata_returns_document_with_issuer() -> None:
    app = _sqlite_mcp(
        host="127.0.0.1",
        port=8000,
        auth_issuer="https://auth.example.com",
    ).streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorization_servers"] == ["https://auth.example.com"]
    assert body["resource"] == "http://127.0.0.1:8000/mcp"
