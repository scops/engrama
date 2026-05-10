"""
Engrama — OpenAI-compatible embedding provider.

One class targets any service that implements the OpenAI
``POST /v1/embeddings`` shape (``{"model": ..., "input": text|[text]}``
returning ``{"data": [{"embedding": [...]}, ...]}``). That covers a
wide ecosystem out of the box:

================  ========================================
Provider          Sample ``base_url``
================  ========================================
OpenAI            ``https://api.openai.com/v1``
Ollama            ``http://localhost:11434/v1``
LM Studio         ``http://localhost:1234/v1``
vLLM              ``http://localhost:8000/v1``
llama.cpp         ``http://localhost:8080/v1``
Jina AI           ``https://api.jina.ai/v1``
DeepSeek          ``https://api.deepseek.com/v1``
OpenRouter        ``https://openrouter.ai/api/v1``
SiliconFlow       ``https://api.siliconflow.cn/v1``
================  ========================================

``api_key`` is optional — local endpoints (Ollama, LM Studio, vLLM)
typically don't require auth. ``dimensions`` is auto-detected from
the first embed response when not supplied.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("engrama.embeddings.openai_compat")


_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "text-embedding-3-small"


class OpenAICompatibleProvider:
    """Embedding provider for any OpenAI-compatible ``/v1/embeddings``.

    Parameters:
        base_url: Service base URL (without trailing slash). Falls back
            to ``OPENAI_BASE_URL`` env var, then OpenAI's default.
        model: Embedding model name. Falls back to ``EMBEDDING_MODEL``.
        api_key: Bearer token. Falls back to ``OPENAI_API_KEY``. Pass
            ``None`` (or leave the env unset) for local endpoints
            that don't require auth.
        dimensions: Expected embedding dimensionality. ``0`` means
            "auto-detect on first embed". Once detected, ``self.dimensions``
            reflects the real value.
        timeout: HTTP timeout in seconds.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        dimensions: int | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url: str = (base_url or os.getenv("OPENAI_BASE_URL", _DEFAULT_BASE_URL)).rstrip(
            "/"
        )
        self.model: str = model or os.getenv("EMBEDDING_MODEL", _DEFAULT_MODEL)
        self.api_key: str | None = api_key or os.getenv("OPENAI_API_KEY")
        if dimensions is None:
            env_dims = os.getenv("EMBEDDING_DIMENSIONS")
            self.dimensions: int = int(env_dims) if env_dims else 0
        else:
            self.dimensions = int(dimensions)
        self._timeout: float = timeout
        self._embed_url: str = f"{self.base_url}/embeddings"
        self._models_url: str = f"{self.base_url}/models"
        self._sync_client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _get_sync_client(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(timeout=self._timeout)
        return self._sync_client

    def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=self._timeout)
        return self._async_client

    @staticmethod
    def _parse_embeddings(payload: dict[str, Any]) -> list[list[float]]:
        """Extract ``[embedding, ...]`` from an OpenAI-shaped response."""
        data = payload.get("data") or []
        return [item["embedding"] for item in data if "embedding" in item]

    def _record_dims(self, vector: list[float]) -> None:
        """Cache the dimensionality on first successful embed."""
        if self.dimensions == 0 and vector:
            self.dimensions = len(vector)

    # ------------------------------------------------------------------
    # Sync API
    # ------------------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        client = self._get_sync_client()
        response = client.post(
            self._embed_url,
            json={"model": self.model, "input": text},
            headers=self._headers(),
        )
        response.raise_for_status()
        embeddings = self._parse_embeddings(response.json())
        if not embeddings:
            raise RuntimeError(f"No embeddings returned for model {self.model!r}")
        self._record_dims(embeddings[0])
        return embeddings[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._get_sync_client()
        response = client.post(
            self._embed_url,
            json={"model": self.model, "input": texts},
            headers=self._headers(),
        )
        response.raise_for_status()
        embeddings = self._parse_embeddings(response.json())
        if len(embeddings) != len(texts):
            raise RuntimeError(f"Expected {len(texts)} embeddings, got {len(embeddings)}")
        if embeddings:
            self._record_dims(embeddings[0])
        return embeddings

    def health_check(self) -> bool:
        """Probe ``GET /models``. Most OpenAI-compat servers expose it."""
        try:
            client = self._get_sync_client()
            response = client.get(
                self._models_url,
                headers=self._headers(),
                timeout=5.0,
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning("Health check failed for %s: %s", self.base_url, e)
            return False

    # ------------------------------------------------------------------
    # Async API
    # ------------------------------------------------------------------

    async def aembed(self, text: str) -> list[float]:
        client = self._get_async_client()
        response = await client.post(
            self._embed_url,
            json={"model": self.model, "input": text},
            headers=self._headers(),
        )
        response.raise_for_status()
        embeddings = self._parse_embeddings(response.json())
        if not embeddings:
            raise RuntimeError(f"No embeddings returned for model {self.model!r}")
        self._record_dims(embeddings[0])
        return embeddings[0]

    async def aembed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._get_async_client()
        response = await client.post(
            self._embed_url,
            json={"model": self.model, "input": texts},
            headers=self._headers(),
        )
        response.raise_for_status()
        embeddings = self._parse_embeddings(response.json())
        if len(embeddings) != len(texts):
            raise RuntimeError(f"Expected {len(texts)} embeddings, got {len(embeddings)}")
        if embeddings:
            self._record_dims(embeddings[0])
        return embeddings

    async def ahealth_check(self) -> bool:
        try:
            client = self._get_async_client()
            response = await client.get(
                self._models_url,
                headers=self._headers(),
                timeout=5.0,
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning("Async health check failed for %s: %s", self.base_url, e)
            return False

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    def __repr__(self) -> str:
        auth = "+key" if self.api_key else "no-auth"
        return (
            f"OpenAICompatibleProvider(base_url={self.base_url!r}, "
            f"model={self.model!r}, dimensions={self.dimensions}, {auth})"
        )
