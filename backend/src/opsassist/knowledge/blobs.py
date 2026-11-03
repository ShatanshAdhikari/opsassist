"""Original document storage. Keys are server-generated, never derived from user input."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Protocol

_KEY = re.compile(r"^[0-9a-f\-]{36}/[0-9a-f\-]{36}$")


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class LocalBlobStore:
    """Filesystem store for development and single-node use. No encryption at rest."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise ValueError("Invalid blob key")
        return self._root / key

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(path)

        await asyncio.to_thread(write)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)
