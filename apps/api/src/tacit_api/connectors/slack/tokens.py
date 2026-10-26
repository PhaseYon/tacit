"""Bot token encryption at rest: AES-256-GCM via connectors/crypto.py.

HKDF info "tacit:slack-bot-token:v1"; the team id is the associated data, so a ciphertext
copied onto another team's row fails to decrypt.
"""

from ..crypto import TokenCipher

_cipher = TokenCipher(b"tacit:slack-bot-token:v1")


def encrypt_token(token: str, team_id: str) -> bytes:
    return _cipher.encrypt(token, team_id)


def decrypt_token(blob: bytes, team_id: str) -> str:
    """Raises cryptography.exceptions.InvalidTag on a wrong key, team or tampering."""
    return _cipher.decrypt(blob, team_id)
