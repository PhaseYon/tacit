"""Backfill and reconcile one connection, one search page at a time (resumable).

Search lists everything shared with the connection, most recently edited first. Each
page of results is recorded, trees are recomputed, changed pages are fetched (block
tree) and upserted, and the cursor is saved, so a crash resumes where it stopped.
- incremental: stops at the first result older than watermark - OVERLAP.
- full: scans everything, then checks pages it didn't see (deleted or unshared).
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from ...db import worker_session
from . import ingest
from . import normalize as n
from .client import (
    NotionClient,
    block_children,
    is_gone,
    list_users,
    retrieve_page,
    search_page,
)
from .sync import Connection, load_users, recompute_trees, upsert_directory

log = logging.getLogger(__name__)

MAX_DEPTH = 8
# Notion's last_edited_time has minute precision: a page fetched less than this long after
# its stamped minute may have changed again within that minute, so it isn't "unchanged".
FRESH_AFTER = timedelta(minutes=2)
OVERLAP = timedelta(minutes=10)
# A run whose cursor hasn't moved for this long is considered dead and may be resumed.
STALE_RUN = timedelta(minutes=30)


# --- Fetching one page ----------------------------------------------------------------


async def fetch_blocks(client: NotionClient, block_id: str, depth: int = 0) -> list[dict]:
    """A block's children, recursively, never descending into child pages/databases
    (those are their own documents)."""
    blocks = await block_children(client, block_id)
    for b in blocks:
        t = b.get("type")
        if not b.get("has_children") or t in ("child_page", "child_database"):
            continue
        if depth + 1 >= MAX_DEPTH:
            continue
        target = b["id"]
        synced_from = (b.get("synced_block") or {}).get("synced_from") or {}
        if t == "synced_block" and synced_from.get("block_id"):
            target = synced_from["block_id"]  # a synced copy: content lives on the original
        try:
            b[n.CHILDREN] = await fetch_blocks(client, target, depth + 1)
        except Exception as exc:
            if not is_gone(exc):
                raise
            b[n.CHILDREN] = []  # e.g. a synced original we can't read: render nothing
    return blocks


def _unchanged(page: dict[str, Any], state: tuple | None) -> bool:
    if state is None:
        return False
    stored, fetched_at, live_doc = state
    edited = n.parse_time(page.get("last_edited_time"))
    return bool(
        live_doc
        and edited
        and stored == edited
        and fetched_at
        and fetched_at > edited + FRESH_AFTER
    )


async def ingest_page(
    conn: Connection,
    client: NotionClient,
    page: dict[str, Any],
    container_id: str | None,
    users: dict[str, str],
) -> bool:
    """Fetch the block tree and upsert. Trashed pages are tombstoned. Returns True if a
    document was written."""
    if n.is_trashed(page):
        await ingest.tombstone(conn.workspace_id, conn.id, [page["id"]])
        return False
    if container_id is None:
        return False
    blocks = await fetch_blocks(client, page["id"])
    doc = n.normalize_page(page, blocks, conn.context(users))
    if doc is None:
        return False
    await ingest.upsert_document(conn.workspace_id, container_id, doc)
    await ingest.mark_fetched(conn.workspace_id, conn.id, page["id"], doc.source_updated_at)
    return True


async def sync_page(conn: Connection, client: NotionClient, page_id: str) -> str:
    """Re-fetch one page (webhooks). Lost access or deleted -> forget it."""
    try:
        page = await retrieve_page(client, page_id)
    except Exception as exc:
        if not is_gone(exc):
            raise
        await ingest.forget_pages(conn.workspace_id, conn.id, [page_id])
        await recompute_trees(conn.workspace_id, conn.id)
        return "done"
    await ingest.record_nodes(conn.workspace_id, conn.id, [page], None)
    containers = await recompute_trees(conn.workspace_id, conn.id)
    users = await load_users(conn.workspace_id, conn.notion_workspace_id)
    await ingest_page(conn, client, page, containers.get(page_id), users)
    return "done"


# --- Runs -----------------------------------------------------------------------------


async def _cursor(workspace_id: str, connection_id: str):
    async with worker_session(workspace_id) as s:
        return (
            await s.execute(
                text(
                    "SELECT mode, next_cursor, run_id::text, run_max_edited, watermark,"
                    " updated_at FROM notion_sync_cursor WHERE connection_id = :c"
                ),
                {"c": connection_id},
            )
        ).one_or_none()


async def _save_cursor(workspace_id: str, connection_id: str, **values: Any) -> None:
    cols = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    updates = ", ".join(f"{k} = excluded.{k}" for k in values)
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                f"INSERT INTO notion_sync_cursor (connection_id, workspace_id, {cols})"
                f" VALUES (:c, :ws, {params}) ON CONFLICT (connection_id) DO UPDATE SET"
                f" {updates}, updated_at = now()"
            ),
            {"c": connection_id, "ws": workspace_id, **values},
        )


def _max(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v]
    return max(present) if present else None


async def run_page(
    conn: Connection, client: NotionClient, *, mode: str = "incremental", continuation: bool = False
) -> bool | None:
    """Process one search page. True = more pages remain in this run; False = the run
    finished; None = skipped (another run of this connection is in flight)."""
    cur = await _cursor(conn.workspace_id, conn.id)
    in_flight = bool(cur and cur.next_cursor)
    if in_flight and not continuation and cur.updated_at > datetime.now(UTC) - STALE_RUN:
        return None
    if in_flight:
        run_id, mode, cursor, run_max = cur.run_id, cur.mode, cur.next_cursor, cur.run_max_edited
    else:
        run_id, cursor, run_max = str(uuid.uuid4()), None, None
        if mode == "full" or cur is None or cur.watermark is None:
            mode = "full"
        await _save_cursor(
            conn.workspace_id, conn.id, mode=mode, run_id=run_id, run_started_at=datetime.now(UTC)
        )
        # Names for page authors and people properties (needs the user-info capability).
        try:
            await upsert_directory(
                conn.workspace_id, conn.notion_workspace_id, await list_users(client)
            )
        except Exception as exc:
            if not is_gone(exc):
                raise
    watermark = cur.watermark if cur else None

    results, next_cursor = await search_page(client, cursor)
    stop_at = watermark - OVERLAP if mode == "incremental" and watermark else None
    if stop_at is not None:
        kept = [
            r for r in results if (n.parse_time(r.get("last_edited_time")) or stop_at) >= stop_at
        ]
        if len(kept) < len(results):
            results, next_cursor = kept, None
    await ingest.record_nodes(conn.workspace_id, conn.id, results, run_id)
    containers = await recompute_trees(conn.workspace_id, conn.id)
    users = await load_users(conn.workspace_id, conn.notion_workspace_id)

    pages = [r for r in results if r.get("object") == "page"]
    states = await ingest.page_states(conn.workspace_id, conn.id, [p["id"] for p in pages])
    for page in pages:
        if not n.is_trashed(page) and _unchanged(page, states.get(page["id"])):
            continue
        await ingest_page(conn, client, page, containers.get(page["id"]), users)
    run_max = _max(run_max, *(n.parse_time(r.get("last_edited_time")) for r in results))

    if next_cursor:
        await _save_cursor(
            conn.workspace_id, conn.id, next_cursor=next_cursor, run_max_edited=run_max
        )
        return True
    if mode == "full":
        await _sweep_unseen(conn, client, run_id, users)
    await _save_cursor(
        conn.workspace_id,
        conn.id,
        next_cursor=None,
        run_max_edited=None,
        watermark=_max(watermark, run_max),
        **({"last_full_at": datetime.now(UTC)} if mode == "full" else {}),
    )
    return False


async def _sweep_unseen(
    conn: Connection, client: NotionClient, run_id: str, users: dict[str, str]
) -> None:
    """After a full scan: anything not seen is deleted, unshared, or was edited mid-scan
    (a descending-sort cursor can skip it). Ask Notion about each page."""
    unseen = await ingest.unseen_pages(conn.workspace_id, conn.id, run_id)
    gone: list[str] = []
    alive: list[dict[str, Any]] = []
    for page_id, obj in unseen:
        if obj != "page":
            gone.append(page_id)  # data sources only matter for trees; next scan re-adds
            continue
        try:
            page = await retrieve_page(client, page_id)
        except Exception as exc:
            if not is_gone(exc):
                raise
            gone.append(page_id)
            continue
        if n.is_trashed(page):
            gone.append(page_id)
        else:
            alive.append(page)
    await ingest.forget_pages(conn.workspace_id, conn.id, gone)
    if alive:
        await ingest.record_nodes(conn.workspace_id, conn.id, alive, run_id)
    containers = await recompute_trees(conn.workspace_id, conn.id)
    for page in alive:
        await ingest_page(conn, client, page, containers.get(page["id"]), users)
