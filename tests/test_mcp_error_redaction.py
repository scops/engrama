"""The MCP error helper must never leak raw exception text to the client.

Raw exception strings can carry DB URIs, filesystem paths and driver internals
(the same class of leak as the fixed Lucene-error). ``_safe_error`` returns a
generic message and keeps the detail in the server logs only.
"""

from __future__ import annotations

import json

from engrama.adapters.mcp.server import _CLIENT_ERROR_MESSAGE, _safe_error


def test_safe_error_hides_exception_detail() -> None:
    exc = RuntimeError("bolt://neo4j:7687 auth failed for user neo4j at /etc/secrets/creds")
    out = json.loads(_safe_error(exc))
    assert out["status"] == "error"
    assert out["error"] == _CLIENT_ERROR_MESSAGE
    blob = json.dumps(out).lower()
    for leak in ("neo4j", "bolt://", "/etc/secrets", "auth failed"):
        assert leak not in blob


def test_safe_error_message_key_and_indent() -> None:
    out = _safe_error(ValueError("boom"), key="message", indent=2)
    d = json.loads(out)
    assert d["status"] == "error"
    assert d["message"] == _CLIENT_ERROR_MESSAGE
    assert "boom" not in out
    assert "\n" in out  # pretty-printed with indent=2
