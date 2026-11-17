"""Build providers from settings. Settings validation enforces the egress approval."""

from __future__ import annotations

from opsassist.config import Settings
from opsassist.llm.anthropic_provider import AnthropicLLMProvider
from opsassist.llm.base import EmbeddingProvider, LLMProvider
from opsassist.llm.hashing import HashEmbeddingProvider
from opsassist.llm.ollama import OllamaEmbeddingProvider, OllamaLLMProvider


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "ollama":
        return OllamaEmbeddingProvider(
            settings.ollama_url,
            settings.embedding_model,
            settings.embedding_timeout_seconds,
            settings.embedding_batch_size,
        )
    return HashEmbeddingProvider()


def build_llm_provider(settings: Settings) -> LLMProvider | None:
    """Return None for `extractive` mode, which answers with excerpts and no model."""
    if settings.llm_provider == "ollama":
        return OllamaLLMProvider(
            settings.ollama_url,
            settings.llm_model,
            settings.llm_timeout_seconds,
            settings.ollama_num_gpu,
            settings.ollama_num_ctx,
        )
    if settings.llm_provider == "anthropic":
        api_key = settings.anthropic_api_key
        return AnthropicLLMProvider(
            settings.llm_model,
            api_key.get_secret_value() if api_key else None,
            settings.llm_timeout_seconds,
        )
    return None
