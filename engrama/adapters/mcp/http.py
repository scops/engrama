"""HTTP-transport concerns for the Engrama MCP server.

This module isolates everything that only matters when the server runs
over **Streamable HTTP** (``ENGRAMA_TRANSPORT=http``) so that
``server.py`` stays focused on the MCP tools themselves:

* Origin / Host allow-list construction for the SDK's built-in
  DNS-rebinding protection (``TransportSecuritySettings``). We do **not**
  write our own middleware — the ``mcp`` SDK already validates the
  ``Origin`` header (bad Origin → 403) and the ``Host`` header
  (bad/missing Host → 421) inside the Streamable HTTP transport.
* The ``/health`` probe endpoint.
* The ``/.well-known/oauth-protected-resource`` (RFC 9728) stub that the
  next phase (OAuth 2.1 against an external issuer) will light up by
  setting ``ENGRAMA_AUTH_ISSUER`` — no code change required.

**Stateless note.** The HTTP server runs with ``stateless_http=True``.
In that mode the SDK creates a fresh transport per request and re-enters
the FastMCP ``lifespan`` on every MCP call, so custom routes registered
here never see the per-request ``lifespan_context``. ``/health`` therefore
owns a small lazily-created, cached store of its own (see
:func:`_build_health_handler`) instead of reaching into the MCP request
context.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("engrama_mcp.http")

# Default Origin allow-list: loopback only, with port wildcards so a real
# browser / desktop client connecting to e.g. http://localhost:8000 is
# accepted out of the box (the SDK supports the ``host:*`` pattern). Override
# with ``ENGRAMA_ALLOWED_ORIGINS`` (CSV).
_DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:*",
    "http://127.0.0.1:*",
)


def parse_origins(csv: str) -> list[str]:
    """Parse a comma-separated ``ENGRAMA_ALLOWED_ORIGINS`` value.

    Empty / whitespace-only entries are dropped. Returns an empty list
    for an empty string so the caller can fall back to the default.
    """
    return [item.strip() for item in csv.split(",") if item.strip()]


def default_allowed_origins() -> list[str]:
    """Loopback-only Origin allow-list used when no override is supplied."""
    return list(_DEFAULT_ALLOWED_ORIGINS)


def derive_allowed_hosts(host: str, port: int) -> list[str]:
    """Build the ``Host`` header allow-list for DNS-rebinding protection.

    The SDK rejects any request whose ``Host`` header is not allow-listed
    (→ 421), so we must explicitly permit the loopback names the server
    binds to. We include the exact ``host:port`` plus the canonical
    loopback aliases and ``:*`` wildcards so that ``localhost`` and
    ``127.0.0.1`` are interchangeable for local clients.
    """
    hosts = {
        f"{host}:{port}",
        f"{host}:*",
        f"localhost:{port}",
        f"127.0.0.1:{port}",
        "localhost:*",
        "127.0.0.1:*",
    }
    return sorted(hosts)


def _build_health_handler(cfg: dict[str, Any]):
    """Return a ``/health`` Starlette handler backed by a cached store.

    The first request lazily creates an async store via the same backend
    factory the MCP server uses; subsequent probes reuse it so a
    Kubernetes liveness probe does not spin up a new Neo4j driver every
    few seconds. The store is closed by process teardown (acceptable for
    a long-lived single server); on a failed health check we drop the
    cached store so a transient outage can recover on the next probe.
    """
    holder: dict[str, Any] = {"store": None}
    lock = asyncio.Lock()

    async def health(request: Request) -> Response:  # noqa: ARG001
        from engrama.backends import create_async_stores

        try:
            async with lock:
                if holder["store"] is None:
                    holder["store"], _ = create_async_stores(cfg)
                store = holder["store"]

            info = await store.health_check()
            ok = info.get("status") == "ok" or bool(info.get("ok"))
            body: dict[str, Any] = {
                "status": "ok" if ok else "error",
                "backend": info.get("backend"),
            }
            if "node_count" in info:
                body["node_count"] = info["node_count"]
            return JSONResponse(body, status_code=200 if ok else 503)
        except Exception as exc:
            logger.warning("Health check failed: %s", exc)
            # Drop the cached store so the next probe rebuilds the
            # connection rather than reusing a broken one.
            stale = holder["store"]
            holder["store"] = None
            if stale is not None and hasattr(stale, "close"):
                try:
                    await stale.close()
                except Exception:
                    pass
            return JSONResponse(
                {"status": "error", "error": str(exc)},
                status_code=503,
            )

    return health


def _build_oauth_metadata_handler(issuer: str | None, resource_url: str):
    """Return the ``/.well-known/oauth-protected-resource`` (RFC 9728) handler.

    This is a **stub** for the upcoming OAuth 2.1 phase. With no issuer
    configured (``ENGRAMA_AUTH_ISSUER`` unset) it returns 404 — the
    resource advertises no authorization server. Once the operator sets
    the issuer, it returns the RFC 9728 metadata document pointing at it,
    so the auth phase only has to set an env var, not touch code.
    """

    async def oauth_protected_resource(request: Request) -> Response:  # noqa: ARG001
        if not issuer:
            return JSONResponse({"error": "not_configured"}, status_code=404)
        return JSONResponse(
            {
                "resource": resource_url,
                "authorization_servers": [issuer],
            },
            status_code=200,
        )

    return oauth_protected_resource


def register_http_routes(
    mcp: Any,
    cfg: dict[str, Any],
    *,
    auth_issuer: str | None,
    host: str,
    port: int,
    mcp_path: str = "/mcp",
) -> None:
    """Register the HTTP-only custom routes on the FastMCP instance.

    These are added to FastMCP's custom-route list and are served by the
    Streamable HTTP Starlette app. They are *not* wrapped by the
    DNS-rebinding middleware (which only guards the MCP endpoint), so a
    probe to ``/health`` with no ``Origin`` header succeeds. Registering
    them in stdio mode is harmless — the routes are never served.
    """
    mcp.custom_route("/health", methods=["GET"])(_build_health_handler(cfg))

    resource_url = f"http://{host}:{port}{mcp_path}"
    mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])(
        _build_oauth_metadata_handler(auth_issuer, resource_url)
    )
