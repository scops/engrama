"""Tests for the Streamable HTTP transport layer of the Engrama MCP server.

These cover the transport-only surface added behind ``ENGRAMA_TRANSPORT=http``:
the stdio↔http switch in the entry point, the session mode, both MCP
protocol eras (``2025-11-25`` handshake and sessionless ``2026-07-28``), the
``/health`` probe, the DNS-rebinding Origin check (bad Origin → 403), and the
RFC 9728 ``/.well-known/oauth-protected-resource`` stub. The server lifespan
runs at app startup, so every test gets its own throwaway SQLite database —
never the user's ``~/.engrama`` one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from engrama.adapters.mcp.server import EngramaMCPServer, create_engrama_mcp

_BASE_URL = "http://127.0.0.1:8000"
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_MODERN = "2026-07-28"
_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": _MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
_TENANT_A = {"X-Engrama-Org-Id": "org-a", "X-Engrama-User-Id": "alice"}
_TENANT_B = {"X-Engrama-Org-Id": "org-b", "X-Engrama-User-Id": "bob"}


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGRAMA_DB_PATH", str(tmp_path / "engrama.db"))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")


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
    """An Engrama MCP server on the (per-test, throwaway) sqlite backend."""
    return create_engrama_mcp(
        backend="sqlite",
        config={"GRAPH_BACKEND": "sqlite"},
        **kwargs,
    )


def _body(resp: Any) -> dict[str, Any]:
    """Decode a Streamable HTTP reply (JSON body or a single SSE event)."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
    return resp.json()


def _modern_post(
    client: TestClient,
    method: str,
    params: dict,
    headers: dict | None = None,
    *,
    path: str = "/mcp",
):
    """POST one self-contained 2026-07-28 request: no handshake, no session."""
    hdrs = {**_MCP_HEADERS, "MCP-Protocol-Version": _MODERN, "Mcp-Method": method}
    if method == "tools/call":
        hdrs["Mcp-Name"] = params["name"]
    return client.post(
        path,
        headers={**hdrs, **(headers or {})},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {**params, "_meta": _MODERN_META},
        },
    )


def _tool_text(resp: Any) -> str:
    result = _body(resp)["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Session mode
# ---------------------------------------------------------------------------


def test_http_is_stateless_by_default() -> None:
    """Handshake-era clients get no Mcp-Session-Id, so any replica can serve
    any request; 2026-07-28 clients have no session at all."""
    server = _sqlite_mcp()
    server.streamable_http_app()
    assert server.session_manager.stateless is True


def test_stateful_http_can_be_opted_in() -> None:
    server = _sqlite_mcp(stateless_http=False)
    server.streamable_http_app()
    assert server.session_manager.stateless is False


def test_streamable_http_app_uses_baked_settings() -> None:
    """Embedders call ``streamable_http_app()`` bare: the path and session
    mode passed to ``create_engrama_mcp`` must still apply."""
    server = _sqlite_mcp(mcp_path="/custom", stateless_http=False)
    app = server.streamable_http_app()
    with TestClient(app, base_url=_BASE_URL) as client:
        resp = _modern_post(client, "server/discover", {}, path="/custom")
    assert resp.status_code == 200
    assert _body(resp)["result"]["supportedVersions"] == [_MODERN]
    assert server.session_manager.stateless is False


# ---------------------------------------------------------------------------
# Protocol eras
# ---------------------------------------------------------------------------


def test_legacy_handshake_is_served_without_session() -> None:
    app = _sqlite_mcp().streamable_http_app()
    with TestClient(app, base_url=_BASE_URL) as client:
        resp = client.post(
            "/mcp",
            headers=_MCP_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy", "version": "0"},
                },
            },
        )
        assert resp.status_code == 200
        assert "mcp-session-id" not in resp.headers
        assert _body(resp)["result"]["protocolVersion"] == "2025-11-25"

        # With no session, the next request stands alone.
        resp = client.post(
            "/mcp",
            headers={**_MCP_HEADERS, "MCP-Protocol-Version": "2025-11-25"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "engrama_status", "arguments": {}},
            },
        )
        assert json.loads(_tool_text(resp))["backend"]


def test_modern_request_needs_no_handshake_or_session() -> None:
    app = _sqlite_mcp().streamable_http_app()
    with TestClient(app, base_url=_BASE_URL) as client:
        discover = _modern_post(client, "server/discover", {})
        call = _modern_post(client, "tools/call", {"name": "engrama_status", "arguments": {}})
    assert discover.status_code == 200
    assert "mcp-session-id" not in discover.headers
    info = _body(discover)["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert info["name"] == "engrama_mcp"
    assert info["version"]
    assert json.loads(_tool_text(call))["backend"]


def test_modern_requests_resolve_tenant_scope_from_headers() -> None:
    """On the sessionless path each request carries its own identity headers;
    a gateway-injected tenant must still see only its own memory."""
    app = _sqlite_mcp().streamable_http_app()
    remember = {
        "name": "engrama_remember",
        "arguments": {"params": {"label": "Concept", "properties": {"name": "only-org-a"}}},
    }
    search = {
        "name": "engrama_search",
        "arguments": {"params": {"query": "only-org-a", "limit": 5}},
    }
    with TestClient(app, base_url=_BASE_URL) as client:
        written = json.loads(_tool_text(_modern_post(client, "tools/call", remember, _TENANT_A)))
        seen_by_a = _tool_text(_modern_post(client, "tools/call", search, _TENANT_A))
        seen_by_b = _tool_text(_modern_post(client, "tools/call", search, _TENANT_B))
    assert (written["node"]["org_id"], written["node"]["user_id"]) == ("org-a", "alice")
    assert json.loads(seen_by_a)["results"][0]["name"] == "only-org-a"
    assert seen_by_b.startswith("No results")


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
    """``ENGRAMA_TRANSPORT`` picks the MCP transport without touching stdio."""
    captured: dict[str, str] = {}

    def fake_run(self: Any, transport: str = "stdio", **kwargs: Any) -> None:  # noqa: ARG001
        captured["transport"] = transport

    monkeypatch.setattr(EngramaMCPServer, "run", fake_run)
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
    """The lifespan runs at startup too: a backend that is down at boot must
    be reported by /health, not abort the server."""
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
