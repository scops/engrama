"""Engrama -- Embedding providers package."""

from engrama.embeddings.null import NullProvider

__all__ = ["NullProvider", "create_provider"]


def create_provider(config=None):
    """Create an embedding provider from configuration."""
    import os

    if config is None:
        config = {}

    provider = (
        config.get("EMBEDDING_PROVIDER")
        or os.getenv("EMBEDDING_PROVIDER", "none")
    )

    if provider == "ollama":
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
        "Supported: ollama, none."
    )
