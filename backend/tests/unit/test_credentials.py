"""Envelope encryption: confidentiality, binding to context, rotation, key configuration."""

from __future__ import annotations

import base64
import os
from dataclasses import replace

import pytest
from pydantic import SecretStr

from opsassist.credentials.crypto import (
    CredentialDecryptError,
    CredentialKeyError,
    KeyRing,
    decode_key,
    generate_key,
)

AAD = b"workspace:credential"


def _ring(active: str = "a", **keys: str) -> KeyRing:
    return KeyRing({key_id: SecretStr(value) for key_id, value in keys.items()}, active)


def test_round_trip_and_ciphertext_hides_plaintext() -> None:
    ring = _ring(a=generate_key())
    secret = ring.encrypt(b"PLAINTEXTCANARY", AAD)
    assert ring.decrypt(secret, AAD) == b"PLAINTEXTCANARY"
    for field in (secret.ciphertext, secret.wrapped_key):
        assert b"PLAINTEXTCANARY" not in field


def test_each_encryption_uses_fresh_keys_and_nonces() -> None:
    ring = _ring(a=generate_key())
    first, second = ring.encrypt(b"same", AAD), ring.encrypt(b"same", AAD)
    assert first.ciphertext != second.ciphertext
    assert first.nonce != second.nonce
    assert first.wrapped_key != second.wrapped_key


def test_row_moved_to_another_context_does_not_decrypt() -> None:
    ring = _ring(a=generate_key())
    secret = ring.encrypt(b"value", AAD)
    with pytest.raises(CredentialDecryptError):
        ring.decrypt(secret, b"other-workspace:credential")


@pytest.mark.parametrize("field", ["ciphertext", "nonce", "wrapped_key", "wrapped_key_nonce"])
def test_altered_row_does_not_decrypt(field: str) -> None:
    ring = _ring(a=generate_key())
    secret = ring.encrypt(b"value", AAD)
    original = getattr(secret, field)
    altered = bytes([original[0] ^ 1]) + original[1:]
    with pytest.raises(CredentialDecryptError):
        ring.decrypt(replace(secret, **{field: altered}), AAD)


def test_wrong_or_missing_key_does_not_decrypt() -> None:
    secret = _ring(a=generate_key()).encrypt(b"value", AAD)
    with pytest.raises(CredentialDecryptError):
        _ring(a=generate_key()).decrypt(secret, AAD)
    with pytest.raises(CredentialDecryptError):
        _ring("b", b=generate_key()).decrypt(secret, AAD)


def test_rewrap_moves_to_active_key_without_touching_ciphertext() -> None:
    old, new = generate_key(), generate_key()
    secret = _ring(a=old).encrypt(b"value", AAD)

    both = _ring("b", a=old, b=new)
    moved = both.rewrap(secret, AAD)
    assert moved.key_id == "b"
    assert moved.ciphertext == secret.ciphertext
    assert moved.wrapped_key != secret.wrapped_key

    # The old key is no longer needed.
    assert _ring("b", b=new).decrypt(moved, AAD) == b"value"


def test_no_active_key_cannot_encrypt() -> None:
    ring = KeyRing({}, None)
    assert not ring.configured
    with pytest.raises(CredentialKeyError):
        ring.encrypt(b"value", AAD)


@pytest.mark.parametrize(
    "value",
    [
        "not base64!!",
        base64.b64encode(b"short").decode(),
        base64.b64encode(os.urandom(16)).decode(),
    ],
)
def test_bad_keys_are_refused_without_quoting_them(value: str) -> None:
    with pytest.raises(CredentialKeyError) as caught:
        decode_key(value)
    assert value not in str(caught.value)


def test_active_key_must_exist() -> None:
    with pytest.raises(CredentialKeyError):
        _ring("missing", a=generate_key())
