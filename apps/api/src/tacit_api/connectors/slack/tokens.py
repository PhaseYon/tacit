"""Bot token encryption at rest (AES-256-GCM).

Key = HKDF-SHA256(BETTER_AUTH_SECRET, info="tacit:slack-bot-token:v1"), so the shared
secret is never used raw. The team id is bound as associated data: a ciphertext copied
onto another team's row fails to decrypt. Format: version byte || 12-byte nonce || ct.
"""

import os
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ...config import get_settings

VERSION = b"\x01"
NONCE_BYTES = 12


@lru_cache
def _key(secret: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"tacit:slack-bot-token:v1"
    ).derive(secret.encode())


def _aead() -> AESGCM:
    return AESGCM(_key(get_settings().better_auth_secret.get_secret_value()))


def encrypt_token(token: str, team_id: str) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return VERSION + nonce + _aead().encrypt(nonce, token.encode(), team_id.encode())


def decrypt_token(blob: bytes, team_id: str) -> str:
    """Raises cryptography.exceptions.InvalidTag on a wrong key, team or tampering."""
    blob = bytes(blob)
    if blob[:1] != VERSION:
        raise ValueError("Unknown token encryption version")
    nonce, ct = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
    return _aead().decrypt(nonce, ct, team_id.encode()).decode()
