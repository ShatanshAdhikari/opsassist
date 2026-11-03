"""Local Ollama adapters."""

from __future__ import annotations

import httpx

from opsassist.llm.base import GenerationResult, ProviderUnavailable
from opsassist.models import EMBEDDING_DIMENSIONS

# nomic-embed-text is trained with task prefixes. Omitting them lowers retrieval quality.
_PREFIXES = {"nomic-embed-text": ("search_document: ", "search_query: ")}


class OllamaEmbeddingProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: float, batch_size: int) -> None:
        self.model = model
        self._url = f"{base_url.rstrip('/')}/api/embed"
        self._timeout = timeout
        self._batch_size = batch_size
        base_name = model.split(":", 1)[0]
        self._document_prefix, self._query_prefix = _PREFIXES.get(base_name, ("", ""))

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._url, json={"model": self.model, "input": inputs})
                response.raise_for_status()
                embeddings: list[list[float]] = response.json()["embeddings"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderUnavailable(f"Ollama embedding failed: {type(exc).__name__}") from exc
        if len(embeddings) != len(inputs):
            raise ProviderUnavailable("Ollama returned the wrong number of embeddings")
        if any(len(vector) != EMBEDDING_DIMENSIONS for vector in embeddings):
            raise ProviderUnavailable(
                f"Embedding model must produce {EMBEDDING_DIMENSIONS} dimensions"
            )
        return embeddings

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            results.extend(await self._embed([self._document_prefix + text for text in batch]))
        return results

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([self._query_prefix + text]))[0]


class OllamaLLMProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: float) -> None:
        self.model = model
        self._url = f"{base_url.rstrip('/')}/api/chat"
        self._timeout = timeout

    async def generate(self, *, system: str, user: str, max_tokens: int) -> GenerationResult:
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": 0.1},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._url, json=payload)
                response.raise_for_status()
                body = response.json()
                text: str = body["message"]["content"]
        except httpx.HTTPStatusError as exc:
            raise ProviderUnavailable(
                f"Ollama generation failed: status {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise ProviderUnavailable(f"Ollama generation failed: {type(exc).__name__}") from exc
        if not text.strip():
            raise ProviderUnavailable("Ollama returned an empty response")
        return GenerationResult(
            text=text,
            input_tokens=body.get("prompt_eval_count"),
            output_tokens=body.get("eval_count"),
        )
