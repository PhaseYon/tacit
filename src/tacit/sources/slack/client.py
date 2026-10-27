"""The only module that imports slack_sdk. Swap the library here if needed.

- Web API: slack_sdk's AsyncWebClient (aiohttp), authenticated with the syncing user's
  own user token (xoxp-). Every non-ok response raises SlackError.
- Rate limits: the SDK's own Retry-After handler. `tacit sync` is a CLI run, so it can
  afford to wait inline, but caps a single wait at MAX_INLINE_WAIT seconds.
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
from slack_sdk.web.async_client import AsyncWebClient

SlackClient = AsyncWebClient
SlackError = SlackApiError
MAX_INLINE_WAIT = 120
PAGE_SIZE = 200  # Slack's recommended maximum for cursor pagination

# User-token scopes (infra/slack/app-manifest.json). Read-only, no DM scopes.
USER_SCOPES = [
    "channels:history",
    "groups:history",
    "channels:read",
    "groups:read",
    "users:read",
]
OPTIONAL_USER_SCOPES = ["users:read.email"]  # lets identity linking match people by email


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
    """Honour Retry-After in-process, but only for waits up to MAX_INLINE_WAIT."""

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


def make_client(token: str, base_url: str) -> AsyncWebClient:
    return AsyncWebClient(
        token=token,
        base_url=base_url,
        timeout=30,
        retry_handlers=[
            AsyncConnectionErrorRetryHandler(max_retry_count=2),
            CappedRateLimitRetryHandler(max_retry_count=5),
            AsyncServerErrorRetryHandler(max_retry_count=2),
        ],
    )


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
