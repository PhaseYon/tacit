"""The only module that imports slack_sdk. Swap the library here if needed.

- Web API: slack_sdk's AsyncWebClient (aiohttp). Every non-ok response raises SlackError.
- Rate limits: the SDK's own Retry-After handler, capped so a worker never sleeps more
  than MAX_INLINE_WAIT seconds. Longer waits raise, and the Celery task retries later.
- Signatures: slack_sdk's SignatureVerifier (v0 HMAC-SHA256, 5-minute timestamp window).
"""

from collections.abc import Awaitable, Callable
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import (
    AsyncConnectionErrorRetryHandler,
    AsyncRateLimitErrorRetryHandler,
    AsyncServerErrorRetryHandler,
)
from slack_sdk.http_retry.request import HttpRequest
from slack_sdk.http_retry.response import HttpResponse
from slack_sdk.http_retry.state import RetryState
from slack_sdk.signature import SignatureVerifier
from slack_sdk.web.async_client import AsyncWebClient

from ...config import get_settings

SlackClient = AsyncWebClient
SlackError = SlackApiError
MAX_INLINE_WAIT = 60
PAGE_SIZE = 200  # Slack's recommended maximum for cursor pagination

BOT_SCOPES = [
    "channels:history",
    "groups:history",
    "channels:read",
    "groups:read",
    "users:read",
    "team:read",
]


class SlackNotConfigured(RuntimeError):
    pass


def _retry_after(headers: dict[str, Any]) -> int | None:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            value = v[0] if isinstance(v, list | tuple) else v
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


class CappedRateLimitRetryHandler(AsyncRateLimitErrorRetryHandler):
    """Honour Retry-After in-process only for short waits."""

    async def _can_retry_async(
        self,
        *,
        state: RetryState,
        request: HttpRequest,
        response: HttpResponse | None = None,
        error: Exception | None = None,
    ) -> bool:
        if not await super()._can_retry_async(
            state=state, request=request, response=response, error=error
        ):
            return False
        assert response is not None
        wait = _retry_after(dict(response.headers))
        return wait is not None and wait <= MAX_INLINE_WAIT


def _client(token: str | None) -> AsyncWebClient:
    return AsyncWebClient(
        token=token,
        base_url=get_settings().slack_api_url,
        timeout=30,
        retry_handlers=[
            AsyncConnectionErrorRetryHandler(max_retry_count=2),
            CappedRateLimitRetryHandler(max_retry_count=2),
            AsyncServerErrorRetryHandler(max_retry_count=2),
        ],
    )


def bot_client(token: str) -> AsyncWebClient:
    return _client(token)


def oauth_client() -> AsyncWebClient:
    """Unauthenticated client for oauth.v2.access (client id + secret go in the call)."""
    return _client(None)


def rate_limit_wait(exc: BaseException) -> int | None:
    """Seconds to wait if `exc` is a Slack 429 the SDK didn't retry in-process."""
    if isinstance(exc, SlackApiError) and getattr(exc.response, "status_code", None) == 429:
        return _retry_after(dict(exc.response.headers or {})) or 60
    return None


def error_code(exc: BaseException) -> str | None:
    if isinstance(exc, SlackApiError):
        return (exc.response.data or {}).get("error") if exc.response is not None else None
    return None


async def get_page(
    method: Callable[..., Awaitable[Any]], key: str, cursor: str | None = None, **params: Any
) -> tuple[list[Any], str | None, dict[str, Any]]:
    """One cursor page: (items, next_cursor, full response data)."""
    if cursor:
        params["cursor"] = cursor
    resp = await method(limit=PAGE_SIZE, **params)
    data = resp.data if hasattr(resp, "data") else resp
    nxt = (data.get("response_metadata") or {}).get("next_cursor") or None
    return list(data.get(key) or []), nxt, data


async def get_all(method: Callable[..., Awaitable[Any]], key: str, **params: Any) -> list[Any]:
    items: list[Any] = []
    cursor: str | None = None
    while True:
        page, cursor, _ = await get_page(method, key, cursor, **params)
        items.extend(page)
        if not cursor:
            return items


def verify_signature(body: bytes, timestamp: str | None, signature: str | None) -> bool:
    secret = get_settings().slack_signing_secret.get_secret_value()
    if not secret or not timestamp or not signature:
        return False
    try:
        return SignatureVerifier(secret).is_valid(body, timestamp, signature)
    except ValueError:  # non-numeric timestamp
        return False
