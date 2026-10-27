"""User config: ~/.config/tacit/config.toml, overridable with TACIT_* env vars.

Secrets never live here; see secrets.py. Env overrides use `__` for nesting, e.g.
TACIT_GRAPH__URI=bolt://localhost:7687. TACIT_CONFIG_DIR moves the whole directory
(tests use it).
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import tomli_w
from platformdirs import user_config_dir
from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


def config_dir() -> Path:
    if env := os.environ.get("TACIT_CONFIG_DIR"):
        return Path(env)
    return Path(user_config_dir("tacit"))


def config_path() -> Path:
    return config_dir() / "config.toml"


class GraphConfig(BaseModel):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    database: str = "neo4j"


class IdentityConfig(BaseModel):
    # Self-asserted; recorded on every access-log entry (design §7).
    email: str = ""


class EmbeddingConfig(BaseModel):
    # "fastembed" (local, default), "openai", or "fake" (tests).
    provider: str = "fastembed"
    model: str = "BAAI/bge-small-en-v1.5"


class LLMConfig(BaseModel):
    # Used only for Graphiti extraction during sync. Off until the user turns it on.
    extract: bool = False
    provider: str = "anthropic"  # anthropic | openai | replay
    model: str = "claude-haiku-5-5"
    small_model: str = "claude-haiku-5-5"
    replay_dir: str = ""
    max_usd_per_run: float = 2.0


class SourcesConfig(BaseModel):
    github_api_url: str = "https://api.github.com"
    slack_api_url: str = "https://slack.com/api/"
    notion_api_url: str = "https://api.notion.com"
    # First sync of a source only looks back this far.
    backfill_days: int = 365


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TACIT_", env_nested_delimiter="__", extra="ignore", env_ignore_empty=True
    )

    graph: GraphConfig = GraphConfig()
    identity: IdentityConfig = IdentityConfig()
    embeddings: EmbeddingConfig = EmbeddingConfig()
    llm: LLMConfig = LLMConfig()
    sources: SourcesConfig = SourcesConfig()
    access_log_days: int = 90

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=config_path()),
        )


@lru_cache
def get_config() -> Config:
    return Config()


def save_config(values: dict[str, Any]) -> Path:
    """Merge `values` (nested dict) into config.toml and reload."""
    path = config_path()
    current: dict[str, Any] = {}
    if path.exists():
        import tomllib

        current = tomllib.loads(path.read_text())
    for section, data in values.items():
        if isinstance(data, dict):
            current.setdefault(section, {}).update(data)
        else:
            current[section] = data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(current))
    get_config.cache_clear()
    return path
