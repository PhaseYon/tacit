from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    # env_ignore_empty: `GITHUB_APP_ID=` in .env means "not set", not "".
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", extra="ignore", env_ignore_empty=True
    )

    api_database_url: str = "postgresql+asyncpg://tacit_api:api@localhost:5432/tacit"
    worker_database_url: str = "postgresql+asyncpg://tacit_worker:worker@localhost:5432/tacit"
    migrator_database_url: str = "postgresql+psycopg://tacit_migrator:migrator@localhost:5432/tacit"
    redis_url: str = "redis://localhost:6379/0"

    auth_jwks_url: str = "http://localhost:3000/api/auth/jwks"
    auth_jwt_issuer: str = "http://localhost:3000"
    auth_jwt_audience: str = "tacit-api"
    # Signs short-lived state tokens (e.g. the GitHub install flow).
    better_auth_secret: SecretStr = SecretStr("dev-only-secret-change-me-in-dotenv-please")

    web_origin: str = "http://localhost:3000"
    api_public_url: str = "http://localhost:8000"

    # GitHub App (see infra/github/README.md). Empty means the connector is disabled.
    github_app_id: int | None = None
    github_app_slug: str = ""
    github_app_client_id: str = ""
    github_app_client_secret: SecretStr = SecretStr("")
    github_app_private_key_path: Path | None = None
    github_webhook_secret: SecretStr = SecretStr("")
    github_api_url: str = "https://api.github.com"
    # "redis" shares installation tokens + ETag cache across workers; "memory" for tests.
    github_cache: str = "redis"

    # Embeddings: "fastembed" (local, free, default), "openai", or "fake" (tests).
    embedding_provider: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    openai_api_key: SecretStr = SecretStr("")

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_app_id and self.github_app_private_key_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
