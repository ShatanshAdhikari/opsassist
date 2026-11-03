"""Deterministic token-hash embeddings: offline and in tests only. No semantic understanding."""

from __future__ import annotations

import hashlib
import math
import re

from opsassist.models import EMBEDDING_DIMENSIONS

_TOKEN = re.compile(r"[a-z0-9]+")


class HashEmbeddingProvider:
    name = "hash"
    model = "hash-v1"

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            # A zero vector has undefined cosine distance. Use a fixed unit vector instead.
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)
