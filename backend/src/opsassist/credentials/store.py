"""Credential storage behind an interface, so a secrets manager can replace it later."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.credentials.crypto import CredentialDecryptError, EncryptedSecret, KeyRing
from opsassist.errors import ServiceUnavailable
from opsassist.models import StoredCredential, utcnow


def _aad(workspace_id: uuid.UUID, credential_id: uuid.UUID) -> bytes:
    return f"opsassist-credential:{workspace_id}:{credential_id}".encode()


def _encrypted(row: StoredCredential) -> EncryptedSecret:
    return EncryptedSecret(
        ciphertext=row.ciphertext,
        nonce=row.nonce,
        wrapped_key=row.wrapped_key,
        wrapped_key_nonce=row.wrapped_key_nonce,
        key_id=row.key_id,
    )


class CredentialStore:
    def __init__(self, keys: KeyRing) -> None:
        self._keys = keys

    def require_configured(self) -> None:
        if not self._keys.configured:
            raise ServiceUnavailable(
                "Credential storage is not configured, so connections cannot be created. Set "
                "CREDENTIAL_KEYS and CREDENTIAL_ACTIVE_KEY_ID."
            )

    async def put(
        self, session: AsyncSession, workspace_id: uuid.UUID, secret: dict[str, Any]
    ) -> uuid.UUID:
        self.require_configured()
        credential_id = uuid.uuid4()
        encrypted = self._keys.encrypt(
            json.dumps(secret).encode(), _aad(workspace_id, credential_id)
        )
        session.add(
            StoredCredential(
                id=credential_id,
                workspace_id=workspace_id,
                ciphertext=encrypted.ciphertext,
                nonce=encrypted.nonce,
                wrapped_key=encrypted.wrapped_key,
                wrapped_key_nonce=encrypted.wrapped_key_nonce,
                key_id=encrypted.key_id,
            )
        )
        await session.flush()
        return credential_id

    async def get(self, session: AsyncSession, credential_id: uuid.UUID) -> dict[str, Any]:
        """RLS limits this to the current workspace. Raises CredentialDecryptError if unusable."""
        row = await session.get(StoredCredential, credential_id)
        if row is None:
            raise CredentialDecryptError("The credential does not exist")
        plaintext = self._keys.decrypt(_encrypted(row), _aad(row.workspace_id, row.id))
        secret: dict[str, Any] = json.loads(plaintext)
        return secret

    async def delete(self, session: AsyncSession, credential_id: uuid.UUID) -> None:
        row = await session.get(StoredCredential, credential_id)
        if row is not None:
            await session.delete(row)

    async def rotate_workspace(self, session: AsyncSession) -> int:
        """Re-wrap this workspace's secrets under the active key. Returns how many moved."""
        self.require_configured()
        active = self._keys.active_key_id
        rows = await session.scalars(
            select(StoredCredential).where(StoredCredential.key_id != active)
        )
        moved = 0
        for row in rows:
            rewrapped = self._keys.rewrap(_encrypted(row), _aad(row.workspace_id, row.id))
            await session.execute(
                update(StoredCredential)
                .where(StoredCredential.id == row.id)
                .values(
                    wrapped_key=rewrapped.wrapped_key,
                    wrapped_key_nonce=rewrapped.wrapped_key_nonce,
                    key_id=rewrapped.key_id,
                    rotated_at=utcnow(),
                )
            )
            moved += 1
        return moved
