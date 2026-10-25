"""Helpers for tests that talk to a mocked GitHub API (respx)."""

from pathlib import Path

import httpx
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

API = "https://api.github.com"


def write_app_key(tmp: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp / "app.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return path


def mock_installation_token(router: respx.MockRouter, installation_id: int) -> None:
    router.post(f"{API}/app/installations/{installation_id}/access_tokens").mock(
        return_value=httpx.Response(
            201,
            json={
                "token": f"ghs_test_{installation_id}",
                "expires_at": "2099-01-01T00:00:00Z",
                "permissions": {},
                "repository_selection": "all",
            },
        )
    )


def repo_json(repo_id: int, name: str, *, private: bool = True, owner: str = "acme") -> dict:
    full = f"{owner}/{name}"
    return {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "name": name,
        "full_name": full,
        "private": private,
        "visibility": "private" if private else "public",
        "html_url": f"https://github.com/{full}",
        "default_branch": "main",
        "archived": False,
    }
