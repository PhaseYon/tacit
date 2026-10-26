"""Backfill and reconcile one channel, one history page at a time (resumable).

conversations.history pages newest-first within [oldest, now]. Each page is upserted and
the cursor saved, so a crash resumes where it stopped. The max ts seen in the run becomes
the new watermark once the last page is done. Thread replies are fetched only for roots
whose latest_reply changed since we last stored them.
"""

import logging
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from ...config import get_settings
from ...db import worker_session
from . import normalize as n
from .client import SlackClient, get_all, get_page
from .ingest import link_orphans, mark_replies_synced, replies_synced, upsert_documents
from .sync import ChannelRef, load_directory

log = logging.getLogger(__name__)

RESOURCE = "history"
RECONCILE_OVERLAP = timedelta(days=2)


def _ts_max(*values: str | None) -> str | None:
    present = [v for v in values if v]
    return max(present, key=float) if present else None


async def _cursor(workspace_id: str, container_id: str):
    async with worker_session(workspace_id) as s:
        return (
            await s.execute(
                text(
                    "SELECT oldest_ts, next_cursor, run_latest_ts FROM slack_sync_cursor"
                    " WHERE container_id = :cid AND resource = :r"
                ),
                {"cid": container_id, "r": RESOURCE},
            )
        ).one_or_none()


async def _save_cursor(
    workspace_id: str,
    container_id: str,
    *,
    oldest_ts: str | None,
    next_cursor: str | None,
    run_latest_ts: str | None,
) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO slack_sync_cursor (container_id, workspace_id, resource, oldest_ts,"
                " next_cursor, run_latest_ts) VALUES (:cid, :ws, :r, :old, :next, :run)"
                " ON CONFLICT (container_id, resource) DO UPDATE SET oldest_ts = excluded.oldest_ts,"
                " next_cursor = excluded.next_cursor, run_latest_ts = excluded.run_latest_ts,"
                " updated_at = now()"
            ),
            {
                "cid": container_id,
                "ws": workspace_id,
                "r": RESOURCE,
                "old": oldest_ts,
                "next": next_cursor,
                "run": run_latest_ts,
            },
        )


async def set_backfill_status(workspace_id: str, container_id: str, status: str) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE slack_channel SET backfill_status = :st,"
                " last_reconciled_at = CASE WHEN :st = 'done' THEN now()"
                " ELSE last_reconciled_at END WHERE container_id = :cid"
            ),
            {"st": status, "cid": container_id},
        )


def _start_oldest(watermark: str | None, *, reconcile: bool) -> str:
    horizon = time.time() - get_settings().slack_backfill_days * 86400
    if watermark is None:
        return f"{horizon:.6f}"
    start = float(watermark) - (RECONCILE_OVERLAP.total_seconds() if reconcile else 0)
    return f"{max(start, horizon):.6f}"


async def backfill_page(
    workspace_id: str, ch: ChannelRef, client: SlackClient, *, reconcile: bool = False
) -> bool:
    """Process one history page. Returns True if more pages remain in this run."""
    cursor = await _cursor(workspace_id, ch.container_id)
    watermark = cursor.oldest_ts if cursor else None
    mid_run = bool(cursor and cursor.next_cursor)
    oldest = _start_oldest(watermark, reconcile=reconcile and not mid_run)
    messages, next_cursor, _ = await get_page(
        client.conversations_history,
        "messages",
        cursor.next_cursor if mid_run else None,
        channel=ch.channel_id,
        oldest=oldest,
        inclusive=True,
    )

    directory = await load_directory(workspace_id, ch.team_id)
    channel = ch.as_channel()
    docs = [n.normalize_message(m, channel, directory) for m in messages]
    await upsert_documents(workspace_id, ch.container_id, docs)
    await _sync_threads(workspace_id, ch, client, messages, directory)

    run_latest = _ts_max(
        cursor.run_latest_ts if mid_run and cursor else None, *(m.get("ts") for m in messages)
    )
    if next_cursor:
        await _save_cursor(
            workspace_id,
            ch.container_id,
            oldest_ts=watermark,
            next_cursor=next_cursor,
            run_latest_ts=run_latest,
        )
        return True
    await _save_cursor(
        workspace_id,
        ch.container_id,
        oldest_ts=_ts_max(watermark, run_latest),
        next_cursor=None,
        run_latest_ts=None,
    )
    await link_orphans(workspace_id, ch.container_id)
    return False


async def _sync_threads(
    workspace_id: str,
    ch: ChannelRef,
    client: SlackClient,
    messages: list[dict[str, Any]],
    directory: n.Directory,
) -> None:
    roots = {
        m["ts"]: m.get("latest_reply")
        for m in messages
        if m.get("reply_count") and m.get("thread_ts") == m.get("ts")
    }
    # Replies only append, so a thread is current if we already fetched it up to its
    # latest_reply (edits/deletes of old replies arrive as events).
    synced = await replies_synced(workspace_id, ch.container_id, list(roots))
    channel = ch.as_channel()
    for root_ts, latest in roots.items():
        if latest and synced.get(root_ts) == latest:
            continue
        replies = await get_all(
            client.conversations_replies, "messages", channel=ch.channel_id, ts=root_ts
        )
        await upsert_documents(
            workspace_id,
            ch.container_id,
            [n.normalize_message(r, channel, directory) for r in replies if r.get("ts") != root_ts],
        )
        await mark_replies_synced(workspace_id, ch.container_id, root_ts, latest)
