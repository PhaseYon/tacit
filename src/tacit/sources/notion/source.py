"""Notion as a Tacit source: one scope entry = one page and its whole subtree.

Uses the team's internal integration token, so only pages shared with the integration
are visible. Each sync:

1. Checks the scope page is still shared (else SourceAccessDenied).
2. Pages through search (pages only, most recently edited first) and stops at the first
   page older than the cursor's watermark (minus a small overlap: Notion's
   last_edited_time has minute precision).
3. Keeps pages that are the scope page or below it, by walking parents (pages, data
   sources, databases, blocks) with a per-run cache.
4. Fetches each kept page's block tree and renders it into one Item.

Cursor: {"last_edited": "<iso>"}, the newest last_edited_time seen. Search runs newest
first, so the watermark can only move once the whole run is done: earlier batches carry
cursor=None (keep the old one) and only the last batch carries the new watermark. A
crash mid-run redoes the run; the content-hash skip makes that cheap.
"""

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from ...config import Config
from ..base import Item, Page, ScopeEntry, SourceAccessDenied, SourceNotConfigured
from . import client as nc
from . import normalize as n

log = logging.getLogger(__name__)

MAX_BLOCK_DEPTH = 8
MAX_PARENT_HOPS = 64
OVERLAP = timedelta(minutes=10)
NOT_SHARED = "share the page with your Tacit integration in Notion (••• → Connections)"


class NotionSource:
    name = "notion"

    def __init__(self, token: str, config: Config) -> None:
        self.token = token
        self.config = config

    def _connect(self):
        return nc.connect(self.token, self.config.sources.notion_api_url)

    # --- resolve ------------------------------------------------------------------------

    async def resolve(self, target: str) -> ScopeEntry:
        page_id = n.parse_page_id(target)
        if page_id is None:
            raise ValueError(
                f"{target!r} isn't a Notion page id or URL. Use notion:<page url> or "
                "notion:<32-character page id>."
            )
        async with self._connect() as client:
            try:
                page = await nc.retrieve(client, "page", page_id)
            except nc.NotionError as exc:
                if nc.is_unauthorized(exc):
                    raise ValueError("Notion rejected the token: run `tacit auth notion`.") from exc
                if nc.is_gone(exc) or nc.is_invalid(exc):
                    raise ValueError(
                        f"Notion page {page_id} wasn't found or isn't shared with the "
                        f"integration: {NOT_SHARED}."
                    ) from exc
                raise
        if n.is_trashed(page):
            raise ValueError(f"Notion page {page_id} is in the trash.")
        return ScopeEntry(
            key=f"notion:{page_id}",
            source="notion",
            target=page_id,
            label=n.title_of(page) or "Untitled",
            options={},
        )

    # --- fetch --------------------------------------------------------------------------

    async def fetch(self, entry: ScopeEntry, cursor: str | None) -> AsyncIterator[Page]:
        root_id = n.dashed(entry.target)
        watermark = _load_cursor(cursor)
        stop_at = (
            watermark - OVERLAP
            if watermark
            else datetime.now(UTC) - timedelta(days=self.config.sources.backfill_days)
        )
        async with self._connect() as client:
            await self._check_root(client, root_id, entry)
            users = await self._users(client)
            walker = _Ancestry(client, root_id)
            newest = watermark
            search_cursor: str | None = None
            while True:
                results, search_cursor = await nc.search_pages(client, search_cursor)
                fresh = [r for r in results if _edited(r) is None or _edited(r) >= stop_at]
                done = search_cursor is None or len(fresh) < len(results)
                items: list[Item] = []
                for page in fresh:
                    if page.get("object") != "page" or n.is_trashed(page):
                        continue
                    if not await walker.in_scope(page):
                        continue
                    item = await self._item(client, page, entry, users)
                    if item is not None:
                        items.append(item)
                newest = _max(newest, *(_edited(r) for r in fresh))
                if done:
                    yield Page(items, _dump_cursor(newest) if newest else None)
                    return
                yield Page(items, None)

    async def _check_root(self, client: nc.NotionClient, root_id: str, entry: ScopeEntry):
        try:
            root = await nc.retrieve(client, "page", root_id)
        except nc.NotionError as exc:
            if nc.is_unauthorized(exc):
                raise SourceNotConfigured(
                    "Notion rejected the token: run `tacit auth notion`."
                ) from exc
            if nc.is_gone(exc):
                raise SourceAccessDenied(
                    f"Notion page {entry.label!r} is no longer shared with the integration: "
                    f"{NOT_SHARED}."
                ) from exc
            raise
        if n.is_trashed(root):
            raise SourceAccessDenied(f"Notion page {entry.label!r} is in the trash.")

    async def _users(self, client: nc.NotionClient) -> dict[str, str]:
        """Notion user id -> name. Needs the integration's "read user information"
        capability; without it, people show up by id."""
        try:
            users = await nc.list_users(client)
        except nc.NotionError as exc:
            if not nc.is_gone(exc):
                raise
            log.info("Notion users.list not allowed for this integration; using ids")
            return {}
        return {u["id"]: u["name"] for u in users if u.get("id") and u.get("name")}

    async def _item(
        self, client: nc.NotionClient, page: dict[str, Any], entry: ScopeEntry, users
    ) -> Item | None:
        try:
            blocks = await fetch_blocks(client, page["id"])
        except nc.NotionError as exc:
            if not nc.is_gone(exc):
                raise
            return None  # deleted or unshared between search and fetch
        return n.normalize_page(page, blocks, scope_key=entry.key, users=users)


async def fetch_blocks(client: nc.NotionClient, block_id: str, depth: int = 0) -> list[dict]:
    """A block's children, recursively (bounded depth), never descending into child
    pages/databases: those are their own items."""
    blocks = await nc.block_children(client, block_id)
    for b in blocks:
        t = b.get("type")
        if not b.get("has_children") or t in ("child_page", "child_database"):
            continue
        if depth + 1 >= MAX_BLOCK_DEPTH:
            continue
        target = b["id"]
        synced_from = (b.get("synced_block") or {}).get("synced_from") or {}
        if t == "synced_block" and synced_from.get("block_id"):
            target = synced_from["block_id"]  # a synced copy: content lives on the original
        try:
            b[n.CHILDREN] = await fetch_blocks(client, target, depth + 1)
        except nc.NotionError as exc:
            if not nc.is_gone(exc):
                raise
            b[n.CHILDREN] = []  # e.g. a synced original we can't read: render nothing
    return blocks


class _Ancestry:
    """Is a page the scope root or below it? Walks parents through the API, caching
    every object's answer for the run. Anything unreadable (workspace, not shared)
    ends the walk: not in scope."""

    def __init__(self, client: nc.NotionClient, root_id: str) -> None:
        self.client = client
        self.root_id = root_id
        self.cache: dict[str, bool] = {f"page:{root_id}": True}  # "<kind>:<id>" -> answer

    async def in_scope(self, page: dict[str, Any]) -> bool:
        chain: list[str] = []
        obj_id = f"page:{page['id']}"
        parent: tuple[str, str | None] | None = _parent(page)
        answer = False
        for _ in range(MAX_PARENT_HOPS):
            if obj_id in self.cache:
                answer = self.cache[obj_id]
                break
            if obj_id in chain:  # a parent loop
                break
            chain.append(obj_id)
            if parent is None:
                parent = await self._parent_of(*_kind_id(obj_id))
            ptype, pid = parent
            if ptype == "workspace" or pid is None:
                break
            obj_id, parent = f"{ptype}:{n.dashed(pid)}", None
            if n.dashed(pid) == self.root_id:
                answer = True
                break
        for key in chain:
            self.cache[key] = answer
        return answer

    async def _parent_of(self, kind: str, object_id: str) -> tuple[str, str | None]:
        try:
            obj = await nc.retrieve(self.client, kind, object_id)
        except nc.NotionError as exc:
            if not (nc.is_gone(exc) or nc.is_invalid(exc)):
                raise
            return ("workspace", None)  # can't see it, so can't place it under the root
        return _parent(obj)


def _parent(obj: dict[str, Any]) -> tuple[str, str | None]:
    """A database row's parent names its data source and (since 2025-09-03) the database
    too; jump straight to the database, whose parent is the page it sits in."""
    raw = obj.get("parent") or {}
    if raw.get("type") == "data_source_id" and raw.get("database_id"):
        return ("database", raw["database_id"])
    return n.parent_ref(obj)


def _kind_id(key: str) -> tuple[str, str]:
    kind, _, oid = key.partition(":")
    return kind, oid


def _edited(obj: dict[str, Any]) -> datetime | None:
    return n.parse_time(obj.get("last_edited_time"))


def _max(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v]
    return max(present) if present else None


def _load_cursor(cursor: str | None) -> datetime | None:
    if not cursor:
        return None
    try:
        return n.parse_time(json.loads(cursor).get("last_edited"))
    except (ValueError, AttributeError):
        log.warning("Ignoring unreadable Notion cursor %r", cursor)
        return None


def _dump_cursor(watermark: datetime) -> str:
    return json.dumps({"last_edited": watermark.isoformat()})
