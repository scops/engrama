"""
Engrama — Ollama embedding provider.

Generates embeddings using a locally-running Ollama instance.
Supports any model served by Ollama that exposes the ``/api/embed``
endpoint (e.g. ``nomic-embed-text``, ``nomic-embed-text-v2-moe``,
``mxbai-embed-large``).

Configuration via environment variables::

    EMBEDDING_PROVIDER=ollama
    EMBEDDING_MODEL=nomic-embed-text
    EMBEDDING_DIMENSIONS=768
    OLLAMA_URL=http://localhost:11434

The provider uses ``urllib`` (stdlib) — no extra dependencies needed.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("engrama.embeddings.ollama")


class OllamaProvider:
    """Embedding provider backed by a local Ollama instance.

    Parameters:
        model: Ollama model name (e.g. ``"nomic-embed-text"``).
        dimensions: Expected embedding dimensionality.  Used for
            validation and exposed as ``self.dimensions``.
        base_url: Ollama server URL.  Defaults to ``OLLAMA_URL``
            env var or ``http://localhost:11434``.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        model: str | None = None,
        dimensions: int | None = None,
        base_url: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.model: str = model or os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
        self.dimensions: int = dimensions or int(os.getenv("EMBEDDING_DIMENSIONS", "768"))
        self._base_url: str = (
            base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")
        ).rstrip("/")
        self._timeout: int = timeout
        self._embed_url: str = f"{self._base_url}/api/embed"
        self._async_client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """Embed a single text string.

        Returns:
            A list of floats with length ``self.dimensions``.

        Raises:
            ConnectionError: If Ollama is not reachable.
            RuntimeError: If the API returns an error.
        """
        result = self._call_api(text)
        embeddings = result.get("embeddings")
        if not embeddings or not embeddings[0]:
            raise RuntimeError(f"Ollama returned no embeddings for model {self.model!r}")
        return embeddings[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of text strings.

        Ollama's ``/api/embed`` endpoint accepts a list of inputs
        natively, so this is a single API call — not a loop.

        Returns:
            A list of embedding vectors, one per input text.
        """
        if not texts:
            return []

        result = self._call_api(texts)
        embeddings = result.get("embeddings", [])

        if len(embeddings) != len(texts):
            raise RuntimeError(f"Expected {len(texts)} embeddings, got {len(embeddings)}")
        return embeddings

    def health_check(self) -> bool:
        """Return ``True`` if Ollama is reachable and the model is available."""
        try:
            # Hit the /api/tags endpoint to check connectivity
            req = urllib.request.Request(
                f"{self._base_url}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                models = [m.get("name", "") for m in data.get("models", [])]
                # Check if our model is available (with or without :latest tag)
                available = any(m == self.model or m.startswith(f"{self.model}:") for m in models)
                if not available:
                    logger.warning(
                        "Ollama is running but model %r not found. Available: %s",
                        self.model,
                        ", ".join(models[:10]),
                    )
                return available
        except (urllib.error.URLError, OSError) as e:
            logger.warning("Ollama health check failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call_api(self, input_: str | list[str]) -> dict[str, Any]:
        """Call the Ollama /api/embed endpoint.

        Args:
            input_: A single string or a list of strings.

        Returns:
            The parsed JSON response.
        """
        payload = json.dumps(
            {
                "model": self.model,
                "input": input_,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            self._embed_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama API error {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise ConnectionError(f"Cannot reach Ollama at {self._base_url}: {e.reason}") from e

    def __repr__(self) -> str:
        return (
            f"OllamaProvider(model={self.model!r}, "
            f"dimensions={self.dimensions}, "
            f"url={self._base_url!r})"
        )

    # ------------------------------------------------------------------
    # Async API (for MCP server — uses httpx)
    # ------------------------------------------------------------------

    async def aembed(self, text: str) -> list[float]:
        """Async embed a single text via POST /api/embed."""
        client = self._get_async_client()
        response = await client.post(
            self._embed_url,
            json={"model": self.model, "input": text},
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings")
        if not embeddings or not embeddings[0]:
            raise RuntimeError(f"Ollama returned no embeddings for model {self.model!r}")
        return embeddings[0]

    async def aembed_batch(self, texts: list[str]) -> list[list[float]]:
        """Async embed a batch of texts."""
        if not texts:
            return []
        client = self._get_async_client()
        response = await client.post(
            self._embed_url,
            json={"model": self.model, "input": texts},
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings", [])
        if len(embeddings) != len(texts):
            raise RuntimeError(f"Expected {len(texts)} embeddings, got {len(embeddings)}")
        return embeddings

    async def ahealth_check(self) -> bool:
        """Async health check — verify Ollama is running and model available."""
        try:
            client = self._get_async_client()
            response = await client.get(
                f"{self._base_url}/api/tags",
                timeout=5.0,
            )
            if response.status_code != 200:
                return False
            data = response.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            available = any(m == self.model or m.startswith(f"{self.model}:") for m in models)
            if not available:
                logger.warning(
                    "Ollama is running but model %r not found. Available: %s",
                    self.model,
                    ", ".join(models[:10]),
                )
            return available
        except Exception as e:
            logger.warning("Ollama async health check failed: %s", e)
            return False

    async def aclose(self) -> None:
        """Close the async HTTP client."""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    def _get_async_client(self) -> httpx.AsyncClient:
        """Lazily create the httpx async client."""
        if self._async_client is None:
            if httpx is None:
                raise ImportError(
                    "httpx is required for async embedding. Install it: pip install httpx"
                )
            self._async_client = httpx.AsyncClient()
        return self._async_client
