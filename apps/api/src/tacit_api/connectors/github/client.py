"""The only module that knows about githubkit. Swap the library here if needed.

- Installation clients mint and cache installation tokens (shared via Redis).
- HTTP caching (ETag / If-None-Match) is on, so conditional 304s don't use rate limit.
- Retries: one rate-limit retry, three 5xx retries. Longer rate-limit waits surface as
  RateLimited so Celery can retry later instead of blocking a worker.
"""

from functools import lru_cache

from githubkit import AppAuthStrategy, GitHub
from githubkit.cache import AsyncRedisCacheStrategy, MemCacheStrategy
from githubkit.exception import PrimaryRateLimitExceeded, SecondaryRateLimitExceeded
from githubkit.retry import RetryChainDecision, RetryRateLimit, RetryServerError
from githubkit.webhooks import verify as _verify
from redis.asyncio import Redis

from ...config import get_settings

RateLimitErrors = (PrimaryRateLimitExceeded, SecondaryRateLimitExceeded)


class GitHubNotConfigured(RuntimeError):
    pass


@lru_cache
def _private_key() -> str:
    s = get_settings()
    if not s.github_enabled:
        raise GitHubNotConfigured("Set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH in .env")
    assert s.github_app_private_key_path is not None
    return s.github_app_private_key_path.read_text()


def _app_auth() -> AppAuthStrategy:
    s = get_settings()
    return AppAuthStrategy(
        s.github_app_id,
        _private_key(),
        s.github_app_client_id or None,
        s.github_app_client_secret.get_secret_value() or None,
    )


def _client_kwargs() -> dict:
    s = get_settings()
    use_redis = s.github_cache == "redis"
    return {
        "base_url": s.github_api_url,
        "http_cache": use_redis,
        "cache_strategy": (
            AsyncRedisCacheStrategy(Redis.from_url(s.redis_url), prefix="githubkit:")
            if use_redis
            else MemCacheStrategy()
        ),
        "auto_retry": RetryChainDecision(
            RetryRateLimit(max_retry=1), RetryServerError(max_retry=3)
        ),
        "timeout": 30.0,
    }


def app_client() -> GitHub:
    """Authenticated as the App itself (JWT): installations, deliveries."""
    return GitHub(_app_auth(), **_client_kwargs())


def installation_client(installation_id: int) -> GitHub:
    """Authenticated as one installation: repo data for that customer."""
    return GitHub(_app_auth().as_installation(installation_id), **_client_kwargs())


def verify_signature(body: bytes, signature_256: str | None) -> bool:
    """Constant-time HMAC check. Requires SHA-256; GitHub's legacy SHA-1 is refused."""
    secret = get_settings().github_webhook_secret.get_secret_value()
    if not secret or not signature_256 or not signature_256.startswith("sha256="):
        return False
    return _verify(secret, body, signature_256)
