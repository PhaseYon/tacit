"""Notion token encryption at rest: AES-256-GCM via connectors/crypto.py.

HKDF info "tacit:notion-token:v1" (a different key from Slack's). Associated data is
"notion:<bot_id>:<kind>", so a ciphertext copied onto another connection, or a refresh
token swapped into the access-token column, fails to decrypt.
"""

from typing import Literal

from ..crypto import TokenCipher

Kind = Literal["access", "refresh"]
_cipher = TokenCipher(b"tacit:notion-token:v1")


def _aad(bot_id: str, kind: Kind) -> str:
    return f"notion:{bot_id}:{kind}"


def encrypt_token(token: str, bot_id: str, kind: Kind) -> bytes:
    return _cipher.encrypt(token, _aad(bot_id, kind))


def decrypt_token(blob: bytes, bot_id: str, kind: Kind) -> str:
    """Raises cryptography.exceptions.InvalidTag on a wrong key, bot, kind or tampering."""
    return _cipher.decrypt(blob, _aad(bot_id, kind))
