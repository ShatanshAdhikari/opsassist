"""Provider protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProviderUnavailable(Exception):
    """The provider could not produce a result: outage, timeout, auth, refusal, or bad response.

    The message is safe to log. It must not contain request content.
    """


@dataclass(frozen=True)
class GenerationResult:
    text: str
    input_tokens: int | None
    output_tokens: int | None


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(
        self, *, system: str, user: str, max_tokens: int, json_output: bool = False
    ) -> GenerationResult:
        """`json_output` asks for JSON where supported. Callers must still validate the reply."""
        ...


class EmbeddingProvider(Protocol):
    name: str
    model: str

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...
