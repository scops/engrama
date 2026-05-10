"""Engrama -- Embedding providers.

Three providers ship in-tree:

* ``"none"`` -- :class:`NullProvider`. Zero-dim, no-op. Default.
* ``"openai"`` -- :class:`OpenAICompatibleProvider`. Talks to any
  ``/v1/embeddings`` endpoint (OpenAI, Ollama via ``/v1``, LM Studio,
  vLLM, llama.cpp, Jina, etc.). The recommended choice for new
  deployments.
* ``"ollama"`` -- :class:`OllamaProvider`. Native ``/api/embed``
  endpoint. Kept for back-compat; new code should prefer ``"openai"``
  with ``OPENAI_BASE_URL=http://localhost:11434/v1``.

Selection happens via the ``EMBEDDING_PROVIDER`` env var (or the
matching key in the config dict).
"""

import os

from engrama.embeddings.null import NullProvider

__all__ = [
    "NullProvider",
    "create_provider",
]


def create_provider(config=None):
    """Create an embedding provider from configuration.

    Parameters:
        config: Optional dict overriding env vars. Recognised keys:
            ``EMBEDDING_PROVIDER``, ``EMBEDDING_MODEL``,
            ``EMBEDDING_DIMENSIONS``, ``OPENAI_BASE_URL``,
            ``OPENAI_API_KEY``, ``OLLAMA_URL``.
    """
    if config is None:
        config = {}

    provider = (
        config.get("EMBEDDING_PROVIDER")
        or os.getenv("EMBEDDING_PROVIDER", "none")
    )

    if provider == "openai":
        from engrama.embeddings.openai_compat import OpenAICompatibleProvider
        return OpenAICompatibleProvider(
            base_url=config.get("OPENAI_BASE_URL"),
            model=config.get("EMBEDDING_MODEL"),
            api_key=config.get("OPENAI_API_KEY"),
            dimensions=(
                int(config["EMBEDDING_DIMENSIONS"])
                if "EMBEDDING_DIMENSIONS" in config
                else None
            ),
        )

    if provider == "ollama":
        # Native /api/embed path (Ollama-only). For the common case of
        # talking to a local Ollama, prefer EMBEDDING_PROVIDER=openai
        # with OPENAI_BASE_URL=http://localhost:11434/v1 so the same
        # code path works against any compatible server.
        from engrama.embeddings.ollama import OllamaProvider
        return OllamaProvider(
            model=config.get("EMBEDDING_MODEL"),
            dimensions=(
                int(config["EMBEDDING_DIMENSIONS"])
                if "EMBEDDING_DIMENSIONS" in config
                else None
            ),
            base_url=config.get("OLLAMA_URL"),
        )

    if provider in ("none", "null"):
        return NullProvider()

    raise ValueError(
        "Unknown embedding provider: " + repr(provider) + ". "
        "Supported: openai, ollama, none."
    )
