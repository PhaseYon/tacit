"""Write Notion pages into source_document, plus per-connection page bookkeeping.

Everything runs as tacit_worker inside one workspace.
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text

from ...db import worker_session
from ..documents import NormalizedDocument
from . import normalize as n

# "Older never overwrites newer". Equal is allowed: Notion's last_edited_time has minute
# precision, so two edits in one minute share a timestamp and the later fetch must win.
UPSERT = text(
    """
    INSERT INTO source_document (
      workspace_id, container_id, source, kind, external_id, url, title, body,
      author_external_id, author_login, source_created_at, source_updated_at, metadata,
      content_hash, token_count)
    VALUES (
      :ws, :cid, 'notion', :kind, :ext, :url, :title, :body, :author_id, :author_login,
      :created, :updated, CAST(:metadata AS jsonb), :hash, :tokens)
    ON CONFLICT (workspace_id, source, external_id) DO UPDATE SET
      container_id = excluded.container_id,
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


async def upsert_document(workspace_id: str, container_id: str, d: NormalizedDocument) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            UPSERT,
            {
                "ws": workspace_id,
                "cid": container_id,
                "kind": d.kind,
                "ext": d.external_id,
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
            },
        )


async def tombstone(workspace_id: str, connection_id: str, page_ids: list[str]) -> None:
    """Hide pages (deleted, trashed, or no longer shared with this connection)."""
    if not page_ids:
        return
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE source_document SET deleted_at = now() WHERE source = 'notion'"
                " AND external_id = ANY(:ids) AND deleted_at IS NULL"
            ),
            {"ids": [f"{connection_id}:{p}" for p in page_ids]},
        )


async def forget_pages(workspace_id: str, connection_id: str, page_ids: list[str]) -> None:
    """Tombstone and drop bookkeeping, so the pages leave their trees."""
    await tombstone(workspace_id, connection_id, page_ids)
    async with worker_session(workspace_id) as s:
        await s.execute(
            text("DELETE FROM notion_page WHERE connection_id = :c AND page_id = ANY(:ids)"),
            {"c": connection_id, "ids": page_ids},
        )


# --- Bookkeeping ----------------------------------------------------------------------


async def record_nodes(
    workspace_id: str, connection_id: str, objs: list[dict[str, Any]], run_id: str | None
) -> None:
    """Remember what search (or a webhook refetch) saw: tree structure and the run.
    last_edited_time is NOT touched here; it records the version we last fetched."""
    rows = []
    for o in objs:
        if o.get("object") not in ("page", "data_source"):
            continue
        ptype, pid = n.parent_ref(o)
        rows.append(
            {
                "c": connection_id,
                "ws": workspace_id,
                "id": o["id"],
                "obj": o["object"],
                "db": (o.get("parent") or {}).get("database_id")
                if o["object"] == "data_source"
                else None,
                "title": n.title_of(o)[: n.TITLE_MAX] or "Untitled",
                "url": o.get("url"),
                "ptype": ptype,
                "pid": pid,
                "run": run_id,
            }
        )
    if not rows:
        return
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO notion_page (connection_id, workspace_id, page_id, object,"
                " database_id, title, url, parent_type, parent_id, seen_run)"
                " VALUES (:c, :ws, :id, :obj, :db, :title, :url, :ptype, :pid,"
                " CAST(:run AS uuid)) ON CONFLICT (connection_id, page_id) DO UPDATE SET"
                " object = excluded.object, database_id = excluded.database_id,"
                " title = excluded.title, url = excluded.url,"
                " parent_type = excluded.parent_type, parent_id = excluded.parent_id,"
                " seen_run = COALESCE(excluded.seen_run, notion_page.seen_run)"
            ),
            rows,
        )


async def page_states(
    workspace_id: str, connection_id: str, page_ids: list[str]
) -> dict[str, tuple[datetime | None, datetime | None, bool]]:
    """page id -> (stored last_edited_time, fetched_at, has a live document)."""
    async with worker_session(workspace_id) as s:
        rows = await s.execute(
            text(
                "SELECT p.page_id, p.last_edited_time, p.fetched_at, EXISTS ("
                "  SELECT 1 FROM source_document d WHERE d.source = 'notion'"
                "  AND d.external_id = p.connection_id::text || ':' || p.page_id"
                "  AND d.deleted_at IS NULL) FROM notion_page p"
                " WHERE p.connection_id = :c AND p.page_id = ANY(:ids)"
            ),
            {"c": connection_id, "ids": page_ids},
        )
        return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def mark_fetched(
    workspace_id: str, connection_id: str, page_id: str, edited: datetime | None
) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE notion_page SET fetched_at = now(), last_edited_time = :e"
                " WHERE connection_id = :c AND page_id = :p"
            ),
            {"c": connection_id, "p": page_id, "e": edited},
        )


async def unseen_pages(workspace_id: str, connection_id: str, run_id: str) -> list[tuple[str, str]]:
    """(page id, object) of everything not seen in this run."""
    async with worker_session(workspace_id) as s:
        rows = await s.execute(
            text(
                "SELECT page_id, object FROM notion_page WHERE connection_id = :c"
                " AND seen_run IS DISTINCT FROM CAST(:run AS uuid)"
            ),
            {"c": connection_id, "run": run_id},
        )
        return [(r[0], r[1]) for r in rows]


async def container_of(workspace_id: str, connection_id: str, page_id: str) -> str | None:
    async with worker_session(workspace_id) as s:
        return await s.scalar(
            text(
                "SELECT container_id::text FROM notion_page WHERE connection_id = :c"
                " AND page_id = :p"
            ),
            {"c": connection_id, "p": page_id},
        )
