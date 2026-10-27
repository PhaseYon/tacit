"""Secrets: env var first, then the OS keychain, then a 0600 file.

The file fallback exists because headless Linux and WSL often have no keychain backend
(the same trade-off the GitHub CLI makes). `tacit doctor` says which store is in use.
"""

import os
import tomllib
from typing import Literal

import keyring
import tomli_w
from keyring.backends import fail
from keyring.errors import KeyringError

from .config import config_dir

SERVICE = "tacit"

Name = Literal[
    "neo4j_password",
    "github_token",
    "slack_token",
    "notion_token",
    "anthropic_api_key",
    "openai_api_key",
]

# Conventional env vars people already have set, checked after TACIT_SECRET_<NAME>.
ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "neo4j_password": ("NEO4J_PASSWORD",),
    "github_token": ("GITHUB_TOKEN", "GH_TOKEN"),
    "slack_token": ("SLACK_TOKEN",),
    "notion_token": ("NOTION_TOKEN",),
    "anthropic_api_key": ("ANTHROPIC_API_KEY",),
    "openai_api_key": ("OPENAI_API_KEY",),
}


class SecretMissing(RuntimeError):
    pass


def _keyring_usable() -> bool:
    if os.environ.get("TACIT_NO_KEYRING"):
        return False
    return not isinstance(keyring.get_keyring(), fail.Keyring)


def _file():
    return config_dir() / "secrets.toml"


def _read_file() -> dict[str, str]:
    path = _file()
    return tomllib.loads(path.read_text()) if path.exists() else {}


def backend() -> str:
    return "keychain" if _keyring_usable() else f"file ({_file()})"


def get(name: Name) -> str | None:
    for var in (f"TACIT_SECRET_{name.upper()}", *ENV_ALIASES.get(name, ())):
        if value := os.environ.get(var):
            return value
    if _keyring_usable():
        try:
            if value := keyring.get_password(SERVICE, name):
                return value
        except KeyringError:
            pass
    return _read_file().get(name)


def require(name: Name, hint: str) -> str:
    value = get(name)
    if not value:
        raise SecretMissing(f"Missing {name}. {hint}")
    return value


def set(name: Name, value: str) -> str:  # noqa: A001 - mirrors keyring's API
    """Store a secret; returns where it went."""
    if _keyring_usable():
        try:
            keyring.set_password(SERVICE, name, value)
            return "keychain"
        except KeyringError:
            pass
    data = _read_file()
    data[name] = value
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    path.write_text(tomli_w.dumps(data))
    return f"file ({path})"


def delete(name: Name) -> None:
    if _keyring_usable():
        try:
            keyring.delete_password(SERVICE, name)
        except KeyringError:
            pass
    data = _read_file()
    if data.pop(name, None) is not None:
        _file().write_text(tomli_w.dumps(data))
