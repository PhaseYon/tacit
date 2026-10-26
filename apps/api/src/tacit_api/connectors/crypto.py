"""Connector token encryption at rest (AES-256-GCM), shared by Slack and Notion.

Key = HKDF-SHA256(BETTER_AUTH_SECRET, info=<per-use label>), so the shared secret is never
used raw and each connector gets its own key. Callers bind the owning row as associated
data (e.g. a team id): a ciphertext copied onto another row fails to decrypt.
Format: version byte || 12-byte nonce || ciphertext+tag.
"""

import os
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..config import get_settings

VERSION = b"\x01"
NONCE_BYTES = 12


@lru_cache
def _key(secret: str, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(secret.encode())


class TokenCipher:
    def __init__(self, info: bytes) -> None:
        self.info = info

    def _aead(self) -> AESGCM:
        return AESGCM(_key(get_settings().better_auth_secret.get_secret_value(), self.info))

    def encrypt(self, token: str, aad: str) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return VERSION + nonce + self._aead().encrypt(nonce, token.encode(), aad.encode())

    def decrypt(self, blob: bytes, aad: str) -> str:
        """Raises cryptography.exceptions.InvalidTag on a wrong key, row or tampering."""
        blob = bytes(blob)
        if blob[:1] != VERSION:
            raise ValueError("Unknown token encryption version")
        nonce, ct = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
        return self._aead().decrypt(nonce, ct, aad.encode()).decode()
