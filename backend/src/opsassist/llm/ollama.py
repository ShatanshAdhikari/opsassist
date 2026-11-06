"""Local Ollama adapters."""

from __future__ import annotations

import httpx

from opsassist.llm.base import GenerationResult, ProviderUnavailable
from opsassist.models import EMBEDDING_DIMENSIONS

# nomic-embed-text is trained with task prefixes. Omitting them lowers retrieval quality.
_PREFIXES = {"nomic-embed-text": ("search_document: ", "search_query: ")}
# Holds full document and tool context, about 8,000 tokens, plus the system prompt and the answer.
DEFAULT_NUM_CTX = 16_384
_CHARS_PER_TOKEN = 4


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

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float,
        num_gpu: int | None = None,
        num_ctx: int = DEFAULT_NUM_CTX,
    ) -> None:
        self.model = model
        self._url = f"{base_url.rstrip('/')}/api/chat"
        self._timeout = timeout
        self._num_gpu = num_gpu
        self._num_ctx = num_ctx

    async def generate(
        self, *, system: str, user: str, max_tokens: int, json_output: bool = False
    ) -> GenerationResult:
        # Ollama silently drops the start of a prompt that exceeds its context, and the model then
        # answers from what is left. Refusing is honest: the caller falls back to excerpts.
        if (len(system) + len(user)) // _CHARS_PER_TOKEN > self._num_ctx:
            raise ProviderUnavailable("The prompt is larger than the model's context")
        options: dict[str, object] = {
            "num_predict": max_tokens,
            "temperature": 0.1,
            "num_ctx": self._num_ctx,
        }
        if self._num_gpu is not None:
            options["num_gpu"] = self._num_gpu
        payload: dict[str, object] = {
            "model": self.model,
            "stream": False,
            "options": options,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_output:
            payload["format"] = "json"
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
