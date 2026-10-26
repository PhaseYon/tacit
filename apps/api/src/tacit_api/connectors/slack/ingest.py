"""Write normalized Slack documents into source_document (as tacit_worker)."""

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import text

from ...db import worker_session
from ..documents import NormalizedDocument

# Same "older never overwrites newer" guard as GitHub. Replies find their thread root by
# external id; a reply that arrives first is relinked by link_orphans.
UPSERT = text(
    """
    INSERT INTO source_document (
      workspace_id, container_id, source, kind, external_id, parent_id, url, title, body,
      author_external_id, author_login, source_created_at, source_updated_at, metadata,
      content_hash, token_count)
    VALUES (
      :ws, :cid, 'slack', :kind, :ext,
      (SELECT p.id FROM source_document p
        WHERE p.workspace_id = :ws AND p.source = 'slack' AND p.external_id = :parent_ext),
      :url, :title, :body, :author_id, :author_login, :created, :updated,
      CAST(:metadata AS jsonb), :hash, :tokens)
    ON CONFLICT (workspace_id, source, external_id) DO UPDATE SET
      container_id = excluded.container_id,
      parent_id = COALESCE(excluded.parent_id, source_document.parent_id),
      url = excluded.url, title = excluded.title, body = excluded.body,
      author_external_id = excluded.author_external_id,
      author_login = COALESCE(excluded.author_login, source_document.author_login),
      source_created_at = excluded.source_created_at,
      source_updated_at = excluded.source_updated_at,
      metadata = source_document.metadata || excluded.metadata,
      content_hash = excluded.content_hash,
      token_count = excluded.token_count,
      deleted_at = NULL,
      updated_at = now()
    WHERE source_document.source_updated_at IS NULL
       OR source_document.source_updated_at <= excluded.source_updated_at
    """
)


def _params(workspace_id: str, container_id: str, d: NormalizedDocument) -> dict[str, Any]:
    return {
        "ws": workspace_id,
        "cid": container_id,
        "kind": d.kind,
        "ext": d.external_id,
        "parent_ext": d.parent_external_id,
        "url": d.url,
        "title": d.title,
        "body": d.body,
        "author_id": d.author_external_id,
        "author_login": d.author_login,
        "created": d.source_created_at,
        "updated": d.source_updated_at,
        "metadata": json.dumps(d.metadata, default=str),
        "hash": d.content_hash,
        "tokens": d.token_count,
    }


async def upsert_documents(
    workspace_id: str, container_id: str, docs: Iterable[NormalizedDocument | None]
) -> int:
    # Roots before replies, so a reply in the same batch finds its parent.
    ordered = sorted((d for d in docs if d is not None), key=lambda d: d.kind != "slack_message")
    rows = [_params(workspace_id, container_id, d) for d in ordered]
    if not rows:
        return 0
    async with worker_session(workspace_id) as s:
        for row in rows:  # one statement per row: later rows may reference earlier ones
            await s.execute(UPSERT, row)
    return len(rows)


async def tombstone(
    workspace_id: str, external_ids: list[str], deleted_at: datetime | None = None
) -> None:
    """Hide deleted messages. `deleted_at` (the deletion event time) also moves
    source_updated_at forward, so a late-arriving older edit can't resurrect it."""
    if not external_ids:
        return
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE source_document SET deleted_at = now(),"
                " source_updated_at = GREATEST(source_updated_at, CAST(:at AS timestamptz))"
                " WHERE source = 'slack' AND external_id = ANY(:ids) AND deleted_at IS NULL"
            ),
            {"ids": external_ids, "at": deleted_at},
        )
        await s.execute(
            text(
                "UPDATE graph_episode SET status = 'stale', updated_at = now() WHERE id IN ("
                " SELECT ged.graph_episode_id FROM graph_episode_document ged"
                " JOIN source_document d ON d.id = ged.document_id"
                " WHERE d.source = 'slack' AND d.external_id = ANY(:ids))"
            ),
            {"ids": external_ids},
        )


async def link_orphans(workspace_id: str, container_id: str) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE source_document c SET parent_id = p.id FROM source_document p"
                " WHERE c.container_id = :cid AND c.source = 'slack' AND c.kind = 'slack_reply'"
                " AND c.parent_id IS NULL AND p.container_id = :cid AND p.source = 'slack'"
                " AND p.kind = 'slack_message' AND p.metadata->>'ts' = c.metadata->>'thread_ts'"
            ),
            {"cid": container_id},
        )


async def replies_synced(
    workspace_id: str, container_id: str, root_ts: list[str]
) -> dict[str, str | None]:
    """thread root ts -> the latest_reply we last fetched replies up to."""
    if not root_ts:
        return {}
    async with worker_session(workspace_id) as s:
        rows = await s.execute(
            text(
                "SELECT metadata->>'ts', metadata->>'replies_synced' FROM source_document"
                " WHERE container_id = :cid AND source = 'slack' AND kind = 'slack_message'"
                " AND metadata->>'ts' = ANY(:ts)"
            ),
            {"cid": container_id, "ts": root_ts},
        )
        return {r[0]: r[1] for r in rows}


async def mark_replies_synced(
    workspace_id: str, container_id: str, root_ts: str, latest: str | None
) -> None:
    # Bookkeeping only: metadata isn't part of content_hash, so nothing is reindexed.
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE source_document SET metadata = metadata || jsonb_build_object("
                "'replies_synced', CAST(:latest AS text)) WHERE container_id = :cid"
                " AND source = 'slack' AND kind = 'slack_message' AND metadata->>'ts' = :ts"
            ),
            {"cid": container_id, "ts": root_ts, "latest": latest},
        )
