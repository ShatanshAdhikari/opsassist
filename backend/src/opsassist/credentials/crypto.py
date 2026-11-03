"""Envelope encryption with AES-256-GCM.

Each secret has its own data key. The data key is encrypted by a key-encryption key (KEK) from
deployment configuration, which never enters the database. Rotating the KEK re-wraps data keys
without touching the secrets themselves.

Associated data binds a ciphertext to its workspace and row. Copying a row to another workspace,
even with database access, produces something that will not decrypt.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

KEY_BYTES = 32
_NONCE_BYTES = 12


class CredentialKeyError(ValueError):
    """Key configuration is unusable. Never includes key material."""


class CredentialDecryptError(Exception):
    """A stored secret could not be decrypted: wrong key, missing key, or altered or moved row."""


@dataclass(frozen=True)
class EncryptedSecret:
    ciphertext: bytes
    nonce: bytes
    wrapped_key: bytes
    wrapped_key_nonce: bytes
    key_id: str


def decode_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialKeyError("A credential key is not valid base64") from exc
    if len(key) != KEY_BYTES:
        raise CredentialKeyError(f"A credential key must be {KEY_BYTES} bytes")
    return key


def generate_key() -> str:
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


class KeyRing:
    """Holds KEKs. Old keys stay configured until rotation has re-wrapped everything under them."""

    def __init__(self, keys: dict[str, SecretStr], active_key_id: str | None) -> None:
        self._keys = {
            key_id: decode_key(value.get_secret_value()) for key_id, value in keys.items()
        }
        self._active = active_key_id
        if active_key_id is not None and active_key_id not in self._keys:
            raise CredentialKeyError("The active credential key id is not configured")

    @property
    def configured(self) -> bool:
        return self._active is not None

    @property
    def active_key_id(self) -> str | None:
        return self._active

    def _wrap(self, data_key: bytes, aad: bytes) -> tuple[bytes, bytes, str]:
        if self._active is None:
            raise CredentialKeyError("No active credential key is configured")
        nonce = os.urandom(_NONCE_BYTES)
        wrapped = AESGCM(self._keys[self._active]).encrypt(nonce, data_key, aad)
        return wrapped, nonce, self._active

    def _unwrap(self, secret: EncryptedSecret, aad: bytes) -> bytes:
        kek = self._keys.get(secret.key_id)
        if kek is None:
            raise CredentialDecryptError("The key for this credential is not configured")
        try:
            return AESGCM(kek).decrypt(secret.wrapped_key_nonce, secret.wrapped_key, aad)
        except InvalidTag as exc:
            raise CredentialDecryptError("The credential could not be decrypted") from exc

    def encrypt(self, plaintext: bytes, aad: bytes) -> EncryptedSecret:
        data_key = AESGCM.generate_key(bit_length=KEY_BYTES * 8)
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, aad)
        wrapped, wrapped_nonce, key_id = self._wrap(data_key, aad)
        return EncryptedSecret(ciphertext, nonce, wrapped, wrapped_nonce, key_id)

    def decrypt(self, secret: EncryptedSecret, aad: bytes) -> bytes:
        data_key = self._unwrap(secret, aad)
        try:
            return AESGCM(data_key).decrypt(secret.nonce, secret.ciphertext, aad)
        except InvalidTag as exc:
            raise CredentialDecryptError("The credential could not be decrypted") from exc

    def rewrap(self, secret: EncryptedSecret, aad: bytes) -> EncryptedSecret:
        """Move a secret to the active KEK. The ciphertext does not change."""
        data_key = self._unwrap(secret, aad)
        wrapped, wrapped_nonce, key_id = self._wrap(data_key, aad)
        return EncryptedSecret(secret.ciphertext, secret.nonce, wrapped, wrapped_nonce, key_id)
