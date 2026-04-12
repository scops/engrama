"""
Tests for Engrama embedding providers.

- Unit tests use a mock HTTP server (no Ollama needed).
- Integration tests hit the real Ollama instance (marked with
  ``@pytest.mark.ollama`` — skipped if Ollama is not running).
"""

from __future__ import annotations

import json
import http.server
import threading
from unittest.mock import patch

import pytest

from engrama.embeddings.null import NullProvider
from engrama.embeddings.ollama import OllamaProvider
from engrama.embeddings.text import node_to_text
from engrama.embeddings import create_provider


# ---------------------------------------------------------------------------
# Helpers — fake Ollama HTTP server
# ---------------------------------------------------------------------------


class _FakeOllamaHandler(http.server.BaseHTTPRequestHandler):
    """Minimal handler that mimics the Ollama /api/embed endpoint."""

    # Class-level config — set by tests before starting the server.
    fake_dims: int = 4
    fail_mode: str | None = None  # "error_500" | "bad_json" | None

    def do_POST(self):
        if self.path == "/api/embed":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))

            if self.fail_mode == "error_500":
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"error": "fake error"}')
                return

            if self.fail_mode == "bad_json":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"not json")
                return

            # Build fake embeddings
            input_ = body.get("input", "")
            if isinstance(input_, str):
                inputs = [input_]
            else:
                inputs = input_

            embeddings = []
            for i, text in enumerate(inputs):
                # Deterministic fake: [len(text)/100, i/10, 0.5, 0.5, ...]
                vec = [len(text) / 100.0, i / 10.0] + [0.5] * (self.fake_dims - 2)
                embeddings.append(vec)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "model": body.get("model", "test"),
                "embeddings": embeddings,
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/api/tags":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "models": [{"name": "nomic-embed-text:latest"}],
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress request logging during tests."""
        pass


@pytest.fixture()
def fake_ollama():
    """Start a fake Ollama server on a random port.

    Yields the base URL (e.g. ``http://127.0.0.1:9999``).
    """
    _FakeOllamaHandler.fail_mode = None
    server = http.server.HTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ---------------------------------------------------------------------------
# NullProvider tests
# ---------------------------------------------------------------------------


class TestNullProvider:
    """Tests for the no-op embedding provider."""

    def test_dimensions_is_zero(self):
        p = NullProvider()
        assert p.dimensions == 0

    def test_embed_returns_empty(self):
        p = NullProvider()
        assert p.embed("hello") == []

    def test_embed_batch_returns_empty_lists(self):
        p = NullProvider()
        result = p.embed_batch(["a", "b", "c"])
        assert result == [[], [], []]

    def test_health_check_always_true(self):
        p = NullProvider()
        assert p.health_check() is True


# ---------------------------------------------------------------------------
# OllamaProvider tests (mocked)
# ---------------------------------------------------------------------------


class TestOllamaProviderMocked:
    """Tests for OllamaProvider against the fake HTTP server."""

    def test_embed_single(self, fake_ollama: str):
        p = OllamaProvider(
            model="nomic-embed-text",
            dimensions=4,
            base_url=fake_ollama,
        )
        vec = p.embed("Hello world")
        assert isinstance(vec, list)
        assert len(vec) == 4
        assert all(isinstance(x, float) for x in vec)

    def test_embed_batch(self, fake_ollama: str):
        p = OllamaProvider(
            model="nomic-embed-text",
            dimensions=4,
            base_url=fake_ollama,
        )
        texts = ["Hello", "World", "Test"]
        result = p.embed_batch(texts)
        assert len(result) == 3
        for vec in result:
            assert len(vec) == 4

    def test_embed_batch_empty(self, fake_ollama: str):
        p = OllamaProvider(
            model="nomic-embed-text",
            dimensions=4,
            base_url=fake_ollama,
        )
        assert p.embed_batch([]) == []

    def test_embed_deterministic_values(self, fake_ollama: str):
        """The fake server returns deterministic embeddings based on text length."""
        p = OllamaProvider(
            model="nomic-embed-text",
            dimensions=4,
            base_url=fake_ollama,
        )
        vec = p.embed("12345")  # len=5
        assert vec[0] == pytest.approx(0.05)  # 5/100
        assert vec[1] == pytest.approx(0.0)   # index 0 / 10

    def test_health_check_succeeds(self, fake_ollama: str):
        p = OllamaProvider(
            model="nomic-embed-text",
            dimensions=4,
            base_url=fake_ollama,
        )
        assert p.health_check() is True

    def test_health_check_wrong_model(self, fake_ollama: str):
        p = OllamaProvider(
            model="nonexistent-model",
            dimensions=4,
            base_url=fake_ollama,
        )
        assert p.health_check() is False

    def test_health_check_unreachable(self):
        p = OllamaProvider(
            model="nomic-embed-text",
            dimensions=4,
            base_url="http://127.0.0.1:1",  # nothing listening
        )
        assert p.health_check() is False

    def test_embed_server_error(self, fake_ollama: str):
        _FakeOllamaHandler.fail_mode = "error_500"
        p = OllamaProvider(
            model="nomic-embed-text",
            dimensions=4,
            base_url=fake_ollama,
        )
        with pytest.raises(RuntimeError, match="Ollama API error 500"):
            p.embed("test")

    def test_embed_unreachable(self):
        p = OllamaProvider(
            model="nomic-embed-text",
            dimensions=4,
            base_url="http://127.0.0.1:1",
        )
        with pytest.raises(ConnectionError, match="Cannot reach Ollama"):
            p.embed("test")

    def test_repr(self, fake_ollama: str):
        p = OllamaProvider(
            model="nomic-embed-text",
            dimensions=768,
            base_url=fake_ollama,
        )
        r = repr(p)
        assert "nomic-embed-text" in r
        assert "768" in r


# ---------------------------------------------------------------------------
# node_to_text tests
# ---------------------------------------------------------------------------


class TestNodeToText:
    """Tests for the text representation helper."""

    def test_basic(self):
        text = node_to_text("Project", {"name": "engrama", "description": "Memory graph"})
        assert text == "Project: engrama Memory graph"

    def test_title_keyed(self):
        text = node_to_text("Decision", {"title": "Use Neo4j", "rationale": "Graph native"})
        assert text == "Decision: Use Neo4j Graph native"

    def test_all_text_fields(self):
        text = node_to_text("Problem", {
            "title": "Auth bug",
            "description": "Token expiry",
            "notes": "Affects mobile",
            "solution": "Refresh tokens",
            "context": "Production",
        })
        assert "Auth bug" in text
        assert "Token expiry" in text
        assert "Affects mobile" in text
        assert "Refresh tokens" in text
        assert "Production" in text

    def test_skips_empty_properties(self):
        text = node_to_text("Technology", {"name": "Python", "notes": None, "version": "3.11"})
        # version is not a text property, notes is None → both skipped
        assert text == "Technology: Python"

    def test_empty_properties(self):
        text = node_to_text("Concept", {})
        assert text == "Concept:"

    def test_non_string_values_converted(self):
        text = node_to_text("Project", {"name": "test", "description": 42})
        assert "42" in text


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestFactory:
    """Tests for the create_provider factory."""

    def test_default_is_null(self):
        p = create_provider({"EMBEDDING_PROVIDER": "none"})
        assert isinstance(p, NullProvider)

    def test_ollama_factory(self, fake_ollama: str):
        p = create_provider({
            "EMBEDDING_PROVIDER": "ollama",
            "EMBEDDING_MODEL": "nomic-embed-text",
            "EMBEDDING_DIMENSIONS": "4",
            "OLLAMA_URL": fake_ollama,
        })
        assert isinstance(p, OllamaProvider)
        assert p.model == "nomic-embed-text"
        assert p.dimensions == 4

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            create_provider({"EMBEDDING_PROVIDER": "nonexistent"})


# ---------------------------------------------------------------------------
# Live Ollama integration tests
# ---------------------------------------------------------------------------


def _ollama_available() -> bool:
    """Check if Ollama is running locally."""
    try:
        p = OllamaProvider()
        return p.health_check()
    except Exception:
        return False


@pytest.mark.skipif(not _ollama_available(), reason="Ollama not running or model not available")
class TestOllamaLive:
    """Integration tests against a real Ollama instance.

    These tests are skipped if Ollama is not running locally with
    nomic-embed-text available.
    """

    def test_embed_single_live(self):
        p = OllamaProvider()
        vec = p.embed("Engrama is a memory graph framework")
        assert isinstance(vec, list)
        assert len(vec) == p.dimensions
        assert all(isinstance(x, float) for x in vec)

    def test_embed_batch_live(self):
        p = OllamaProvider()
        texts = [
            "Python programming language",
            "Neo4j graph database",
            "Ethical hacking and penetration testing",
        ]
        result = p.embed_batch(texts)
        assert len(result) == 3
        for vec in result:
            assert len(vec) == p.dimensions

    def test_different_texts_different_embeddings(self):
        p = OllamaProvider()
        vec_a = p.embed("Artificial intelligence")
        vec_b = p.embed("Chocolate cake recipe")
        # Cosine similarity should be low — just check they're not identical
        assert vec_a != vec_b

    def test_node_to_text_integration(self):
        """End-to-end: node → text → embedding."""
        p = OllamaProvider()
        text = node_to_text("Project", {
            "name": "engrama",
            "description": "Graph-based long-term memory for AI agents",
        })
        vec = p.embed(text)
        assert len(vec) == p.dimensions

    def test_health_check_live(self):
        p = OllamaProvider()
        assert p.health_check() is True
