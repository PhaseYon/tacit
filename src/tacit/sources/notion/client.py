"""The only module that imports notion_client. Swap the library here if needed.

- API: notion_client's AsyncClient (httpx), Notion-Version 2025-09-03, authenticated with
  the team's internal integration token (it sees only pages shared with the integration).
- Rate limits: the SDK retries 429s (honouring Retry-After) and GET 500/503 itself.
  `call()` waits out a 429 the SDK gave up on a few more times before raising.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from notion_client import AsyncClient
from notion_client.client import RetryOptions
from notion_client.errors import APIErrorCode, APIResponseError
from notion_client.helpers import async_collect_paginated_api

NotionClient = AsyncClient
NotionError = APIResponseError
NOTION_VERSION = "2025-09-03"
PAGE_SIZE = 100  # Notion's maximum
RATE_LIMIT_ATTEMPTS = 3
MAX_WAIT_S = 120


def _client(token: str, base_url: str) -> AsyncClient:
    return AsyncClient(
        auth=token,
        base_url=base_url.rstrip("/"),
        notion_version=NOTION_VERSION,
        timeout_ms=30_000,
        retry=RetryOptions(max_retries=3, max_retry_delay_ms=60_000),
    )


@asynccontextmanager
async def connect(token: str, base_url: str) -> AsyncIterator[AsyncClient]:
    client = _client(token, base_url)
    try:
        yield client
    finally:
        await client.aclose()


# --- Errors ---------------------------------------------------------------------------


def _code(exc: BaseException) -> str | None:
    if not isinstance(exc, APIResponseError):
        return None
    return str(getattr(exc.code, "value", exc.code))  # a plain str from the response body


def is_unauthorized(exc: BaseException) -> bool:
    """The token is invalid, expired or revoked."""
    return _code(exc) == APIErrorCode.Unauthorized.value


def is_gone(exc: BaseException) -> bool:
    """The object doesn't exist for this token: deleted, or no longer shared with it."""
    return _code(exc) in (APIErrorCode.ObjectNotFound.value, APIErrorCode.RestrictedResource.value)


def is_invalid(exc: BaseException) -> bool:
    """A 400: e.g. a malformed id, or the id of a different kind of object."""
    return _code(exc) == APIErrorCode.ValidationError.value


def rate_limit_wait(exc: BaseException) -> int | None:
    """Seconds to wait if `exc` is a 429 the SDK gave up retrying."""
    if _code(exc) != APIErrorCode.RateLimited.value:
        return None
    try:
        return int(exc.headers.get("retry-after") or 60)  # type: ignore[attr-defined]
    except ValueError:
        return 60


async def call[T](fn: Callable[[], Awaitable[T]]) -> T:
    """Run one API call, waiting out rate limits the SDK's own retries didn't absorb."""
    for attempt in range(RATE_LIMIT_ATTEMPTS):
        try:
            return await fn()
        except APIResponseError as exc:
            wait = rate_limit_wait(exc)
            if wait is None or attempt == RATE_LIMIT_ATTEMPTS - 1:
                raise
            await asyncio.sleep(min(wait, MAX_WAIT_S))
    raise AssertionError("unreachable")


# --- Reads ----------------------------------------------------------------------------


async def search_pages(client: AsyncClient, cursor: str | None) -> tuple[list[dict], str | None]:
    """One page of pages shared with the integration, most recently edited first."""
    params: dict[str, Any] = {
        "page_size": PAGE_SIZE,
        "filter": {"property": "object", "value": "page"},
        "sort": {"timestamp": "last_edited_time", "direction": "descending"},
    }
    if cursor:
        params["start_cursor"] = cursor
    resp = await call(lambda: client.search(**params))
    nxt = resp.get("next_cursor") if resp.get("has_more") else None
    return list(resp.get("results") or []), nxt


async def retrieve(client: AsyncClient, kind: str, object_id: str) -> dict[str, Any]:
    """GET one page, block, database or data source by id."""
    endpoints: dict[str, Callable[[], Awaitable[Any]]] = {
        "page": lambda: client.pages.retrieve(page_id=object_id),
        "block": lambda: client.blocks.retrieve(block_id=object_id),
        "database": lambda: client.databases.retrieve(database_id=object_id),
        "data_source": lambda: client.data_sources.retrieve(data_source_id=object_id),
    }
    return await call(endpoints[kind])


async def block_children(client: AsyncClient, block_id: str) -> list[dict[str, Any]]:
    return await call(
        lambda: async_collect_paginated_api(
            client.blocks.children.list, block_id=block_id, page_size=PAGE_SIZE
        )
    )


async def list_users(client: AsyncClient) -> list[dict[str, Any]]:
    return await call(lambda: async_collect_paginated_api(client.users.list, page_size=PAGE_SIZE))
