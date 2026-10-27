"""The only module that imports notion_client. Swap the library here if needed.

- API: notion_client's AsyncClient (httpx), Notion-Version 2025-09-03.
- Rate limits: the SDK retries 429s (honouring Retry-After, capped at 60 s) and GET
  500/503 itself. What survives raises, and the Celery task retries later.
- Webhook signatures: HMAC-SHA256 of the raw body keyed with the verification token
  (`X-Notion-Signature: sha256=<hex>`). The SDK has no verifier; this is the whole thing.
"""

import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from notion_client import AsyncClient
from notion_client.client import RetryOptions
from notion_client.errors import APIErrorCode, APIResponseError
from notion_client.helpers import async_collect_paginated_api

from ...config import get_settings

NotionClient = AsyncClient
NotionError = APIResponseError
NOTION_VERSION = "2025-09-03"
PAGE_SIZE = 100  # Notion's maximum


class NotionNotConfigured(RuntimeError):
    pass


def _client(token: str | None) -> AsyncClient:
    return AsyncClient(
        auth=token,
        base_url=get_settings().notion_api_url,
        notion_version=NOTION_VERSION,
        timeout_ms=30_000,
        retry=RetryOptions(max_retries=2, max_retry_delay_ms=60_000),
    )


@asynccontextmanager
async def connect(token: str) -> AsyncIterator[AsyncClient]:
    client = _client(token)
    try:
        yield client
    finally:
        await client.aclose()


async def exchange(grant: dict[str, Any]) -> dict[str, Any]:
    """POST /v1/oauth/token (Basic auth). `grant` is the body: an authorization_code or
    refresh_token grant."""
    s = get_settings()
    if not s.notion_enabled:
        raise NotionNotConfigured("Notion integration is not configured")
    client = _client(None)
    try:
        return await client.oauth.token(
            client_id=s.notion_client_id,
            client_secret=s.notion_client_secret.get_secret_value(),
            **grant,
        )
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


def rate_limit_wait(exc: BaseException) -> int | None:
    """Seconds to wait if `exc` is a 429 the SDK gave up retrying."""
    if _code(exc) != APIErrorCode.RateLimited.value:
        return None
    try:
        return int(exc.headers.get("retry-after") or 60)  # type: ignore[attr-defined]
    except ValueError:
        return 60


# --- Reads ----------------------------------------------------------------------------


async def search_page(client: AsyncClient, cursor: str | None) -> tuple[list[dict], str | None]:
    """One page of everything shared with this connection, most recently edited first."""
    params: dict[str, Any] = {
        "page_size": PAGE_SIZE,
        "sort": {"timestamp": "last_edited_time", "direction": "descending"},
    }
    if cursor:
        params["start_cursor"] = cursor
    resp = await client.search(**params)
    nxt = resp.get("next_cursor") if resp.get("has_more") else None
    return list(resp.get("results") or []), nxt


async def retrieve_page(client: AsyncClient, page_id: str) -> dict[str, Any]:
    return await client.pages.retrieve(page_id=page_id)


async def block_children(client: AsyncClient, block_id: str) -> list[dict[str, Any]]:
    return await async_collect_paginated_api(
        client.blocks.children.list, block_id=block_id, page_size=PAGE_SIZE
    )


async def list_users(client: AsyncClient) -> list[dict[str, Any]]:
    return await async_collect_paginated_api(client.users.list, page_size=PAGE_SIZE)


def verify_signature(body: bytes, signature: str | None) -> bool:
    secret = get_settings().notion_webhook_verification_token.get_secret_value()
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
