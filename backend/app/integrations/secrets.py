"""AES-GCM envelopes for channel, webhook and OAuth credentials."""

from __future__ import annotations

import base64
import importlib
import os
from dataclasses import dataclass
from typing import Protocol, cast

NONCE_BYTES = 12
SUPPORTED_KEY_BYTES = frozenset({16, 24, 32})


class SecretCipherError(ValueError):
    pass


class _AESGCM(Protocol):
    def encrypt(self, nonce: bytes, data: bytes, associated_data: bytes) -> bytes: ...

    def decrypt(self, nonce: bytes, data: bytes, associated_data: bytes) -> bytes: ...


class _AESGCMFactory(Protocol):
    def __call__(self, key: bytes) -> _AESGCM: ...


@dataclass(frozen=True, slots=True)
class SecretCipher:
    key: bytes
    key_id: str = "primary"

    def __post_init__(self) -> None:
        if len(self.key) not in SUPPORTED_KEY_BYTES:
            raise SecretCipherError("AES key must contain 16, 24 or 32 bytes")

    @classmethod
    def from_base64(cls, encoded_key: str, *, key_id: str = "primary") -> SecretCipher:
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except (ValueError, TypeError) as exc:
            raise SecretCipherError("encryption key must be valid base64") from exc
        return cls(key=key, key_id=key_id)

    def encrypt(self, plaintext: bytes | str, *, associated_data: bytes) -> bytes:
        aesgcm_factory = _aesgcm_factory()
        raw = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        nonce = os.urandom(NONCE_BYTES)
        return b"\x01" + nonce + aesgcm_factory(self.key).encrypt(nonce, raw, associated_data)

    def decrypt(self, envelope: bytes, *, associated_data: bytes) -> bytes:
        if len(envelope) <= 1 + NONCE_BYTES or envelope[0] != 1:
            raise SecretCipherError("unsupported encrypted secret envelope")
        aesgcm_factory = _aesgcm_factory()
        nonce = envelope[1 : 1 + NONCE_BYTES]
        ciphertext = envelope[1 + NONCE_BYTES :]
        try:
            return aesgcm_factory(self.key).decrypt(nonce, ciphertext, associated_data)
        except Exception as exc:
            # Do not expose whether the key, tag or associated data was wrong.
            raise SecretCipherError("encrypted secret cannot be decrypted") from exc


def _aesgcm_factory() -> _AESGCMFactory:
    try:
        module = importlib.import_module("cryptography.hazmat.primitives.ciphers.aead")
    except ModuleNotFoundError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError("install cryptography to encrypt integration credentials") from exc
    return cast(_AESGCMFactory, module.__dict__["AESGCM"])
