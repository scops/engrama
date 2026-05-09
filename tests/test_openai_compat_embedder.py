"""Tests for OpenAICompatibleProvider.

Uses ``httpx.MockTransport`` so no real HTTP traffic happens — fast,
deterministic, and runs without any external service.
"""

from __future__ import annotations

import pytest
import httpx

from engrama.embeddings import create_provider
from engrama.embeddings.openai_compat import OpenAICompatibleProvider


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _make_handler(captured: list[dict] | None = None, dims: int = 4):
    """Build a callable that fakes the OpenAI /v1/embeddings endpoint."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            payload = request.read()
            import json as _json
            body = _json.loads(payload)
            if captured is not None:
                captured.append({
                    "headers": dict(request.headers),
                    "body": body,
                })
            input_ = body["input"]
            if isinstance(input_, str):
                inputs = [input_]
            else:
                inputs = input_
            data = []
            for i, _ in enumerate(inputs):
                # Deterministic vector: position-encoded.
                vec = [float((i + 1) * (j + 1) % 7) for j in range(dims)]
                data.append({"embedding": vec, "index": i})
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": data,
                    "model": body["model"],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": "demo-model"}]},
            )
        return httpx.Response(404, json={"error": "not found"})
    return handler


def _make_provider(transport: httpx.MockTransport, **kwargs) -> OpenAICompatibleProvider:
    p = OpenAICompatibleProvider(
        base_url=kwargs.pop("base_url", "https://example.test/v1"),
        model=kwargs.pop("model", "demo-model"),
        api_key=kwargs.pop("api_key", None),
        dimensions=kwargs.pop("dimensions", None),
    )
    # Inject the mock transport into both sync + async clients.
    p._sync_client = httpx.Client(transport=transport, timeout=p._timeout)
    p._async_client = httpx.AsyncClient(transport=transport, timeout=p._timeout)
    return p


# ----------------------------------------------------------------------
# Sync API
# ----------------------------------------------------------------------


def test_embed_returns_vector_and_records_dims(monkeypatch):
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    transport = httpx.MockTransport(_make_handler(dims=4))
    p = _make_provider(transport)
    assert p.dimensions == 0
    vec = p.embed("hello")
    assert isinstance(vec, list) and len(vec) == 4
    assert p.dimensions == 4


def test_embed_batch_matches_input_count():
    transport = httpx.MockTransport(_make_handler(dims=3))
    p = _make_provider(transport)
    vecs = p.embed_batch(["a", "b", "c"])
    assert len(vecs) == 3
    assert all(len(v) == 3 for v in vecs)


def test_embed_batch_empty_short_circuit():
    """Empty input should not hit the network."""
    captured: list = []
    transport = httpx.MockTransport(_make_handler(captured=captured))
    p = _make_provider(transport)
    assert p.embed_batch([]) == []
    assert captured == []


def test_health_check_pings_models_endpoint():
    transport = httpx.MockTransport(_make_handler())
    p = _make_provider(transport)
    assert p.health_check() is True


def test_health_check_returns_false_on_unreachable():
    def boom(request):
        raise httpx.ConnectError("nope")
    transport = httpx.MockTransport(boom)
    p = _make_provider(transport)
    assert p.health_check() is False


def test_api_key_sets_authorization_header():
    captured: list = []
    transport = httpx.MockTransport(_make_handler(captured=captured))
    p = _make_provider(transport, api_key="sk-test-123")
    p.embed("hi")
    assert captured[0]["headers"]["authorization"] == "Bearer sk-test-123"


def test_no_api_key_omits_authorization():
    """Local endpoints (Ollama, LM Studio) don't require auth."""
    captured: list = []
    transport = httpx.MockTransport(_make_handler(captured=captured))
    p = _make_provider(transport, api_key=None)
    p.embed("hi")
    assert "authorization" not in captured[0]["headers"]


def test_explicit_dimensions_not_overwritten_by_response():
    """User-supplied dims wins; auto-detect only when 0."""
    transport = httpx.MockTransport(_make_handler(dims=4))
    p = _make_provider(transport, dimensions=128)
    assert p.dimensions == 128
    p.embed("hi")
    assert p.dimensions == 128  # unchanged


def test_empty_response_raises():
    def empty(request):
        return httpx.Response(200, json={"object": "list", "data": []})
    transport = httpx.MockTransport(empty)
    p = _make_provider(transport)
    with pytest.raises(RuntimeError, match="No embeddings"):
        p.embed("hi")


def test_batch_count_mismatch_raises():
    def short(request):
        return httpx.Response(200, json={"object": "list", "data": [
            {"embedding": [1.0, 2.0]},
        ]})
    transport = httpx.MockTransport(short)
    p = _make_provider(transport)
    with pytest.raises(RuntimeError, match="Expected 3"):
        p.embed_batch(["a", "b", "c"])


def test_repr_indicates_auth_state():
    p = _make_provider(httpx.MockTransport(_make_handler()), api_key="sk-x")
    assert "+key" in repr(p)
    p2 = _make_provider(httpx.MockTransport(_make_handler()), api_key=None)
    assert "no-auth" in repr(p2)


# ----------------------------------------------------------------------
# Async API
# ----------------------------------------------------------------------


async def test_aembed_returns_vector(monkeypatch):
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    transport = httpx.MockTransport(_make_handler(dims=5))
    p = _make_provider(transport)
    vec = await p.aembed("hello")
    assert len(vec) == 5
    assert p.dimensions == 5


async def test_aembed_batch_count():
    transport = httpx.MockTransport(_make_handler(dims=2))
    p = _make_provider(transport)
    vecs = await p.aembed_batch(["x", "y"])
    assert len(vecs) == 2 and all(len(v) == 2 for v in vecs)


async def test_ahealth_check():
    transport = httpx.MockTransport(_make_handler())
    p = _make_provider(transport)
    assert await p.ahealth_check() is True


async def test_aclose_releases_clients():
    transport = httpx.MockTransport(_make_handler())
    p = _make_provider(transport)
    await p.aembed("hi")
    await p.aclose()
    assert p._async_client is None
    assert p._sync_client is None


# ----------------------------------------------------------------------
# Factory wiring
# ----------------------------------------------------------------------


def test_create_provider_openai(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    p = create_provider({
        "EMBEDDING_PROVIDER": "openai",
        "OPENAI_BASE_URL": "http://localhost:11434/v1",
        "EMBEDDING_MODEL": "nomic-embed-text",
        "OPENAI_API_KEY": None,
    })
    assert isinstance(p, OpenAICompatibleProvider)
    assert p.base_url == "http://localhost:11434/v1"
    assert p.model == "nomic-embed-text"


def test_create_provider_passes_dimensions(monkeypatch):
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    p = create_provider({
        "EMBEDDING_PROVIDER": "openai",
        "EMBEDDING_DIMENSIONS": "768",
    })
    assert p.dimensions == 768


def test_create_provider_unknown_raises():
    with pytest.raises(ValueError, match="Unknown embedding provider"):
        create_provider({"EMBEDDING_PROVIDER": "made-up"})
