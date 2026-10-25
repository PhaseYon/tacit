"""Write normalized GitHub documents into source_document (as tacit_worker)."""

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import worker_session
from ..documents import NormalizedDocument

# Older events never overwrite newer data: the update only applies when the incoming
# source_updated_at is at least as new as the stored one.
UPSERT = text(
    """
    INSERT INTO source_document (
      workspace_id, container_id, source, kind, external_id, parent_id, url, title, body,
      author_external_id, author_login, source_created_at, source_updated_at, metadata,
      content_hash, token_count)
    VALUES (
      :ws, :cid, 'github', :kind, :ext,
      (SELECT p.id FROM source_document p
        WHERE p.container_id = :cid AND p.kind IN ('issue', 'pull_request')
          AND (p.metadata->>'number')::int = :parent_number
        LIMIT 1),
      :url, :title, :body, :author_id, :author_login, :created, :updated,
      CAST(:metadata AS jsonb), :hash, :tokens)
    ON CONFLICT (workspace_id, source, external_id) DO UPDATE SET
      container_id = excluded.container_id,
      parent_id = COALESCE(excluded.parent_id, source_document.parent_id),
      url = excluded.url, title = excluded.title, body = excluded.body,
      author_external_id = excluded.author_external_id,
      author_login = excluded.author_login,
      source_created_at = excluded.source_created_at,
      source_updated_at = excluded.source_updated_at,
      metadata = source_document.metadata || excluded.metadata,
      content_hash = excluded.content_hash,
      token_count = excluded.token_count,
      deleted_at = NULL,
      updated_at = now()
    WHERE source_document.source_updated_at IS NULL
       OR excluded.source_updated_at IS NULL
       OR source_document.source_updated_at <= excluded.source_updated_at
    """
)


def _params(workspace_id: str, container_id: str, d: NormalizedDocument) -> dict[str, Any]:
    return {
        "ws": workspace_id,
        "cid": container_id,
        "kind": d.kind,
        "ext": d.external_id,
        "parent_number": d.metadata.get("parent_number"),
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
    rows = [_params(workspace_id, container_id, d) for d in docs if d is not None]
    if not rows:
        return 0
    async with worker_session(workspace_id) as s:
        await s.execute(UPSERT, rows)
        await _upsert_identities(s, workspace_id, rows)
    return len(rows)


async def _upsert_identities(s: AsyncSession, workspace_id: str, rows: list[dict]) -> None:
    people = {r["author_id"]: r["author_login"] for r in rows if r["author_id"]}
    if not people:
        return
    await s.execute(
        text(
            "INSERT INTO external_identity (workspace_id, source, external_id, login)"
            " VALUES (:ws, 'github', :ext, :login)"
            " ON CONFLICT (workspace_id, source, external_id)"
            " DO UPDATE SET login = excluded.login, updated_at = now()"
        ),
        [{"ws": workspace_id, "ext": k, "login": v} for k, v in people.items()],
    )


async def tombstone(workspace_id: str, external_ids: list[str]) -> None:
    if not external_ids:
        return
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE source_document SET deleted_at = now()"
                " WHERE source = 'github' AND external_id = ANY(:ids) AND deleted_at IS NULL"
            ),
            {"ids": external_ids},
        )
        # Facts from a bundle that contained a deleted document are hidden at once
        # (graph reads require status='done'); the graph worker rebuilds it.
        await s.execute(
            text(
                "UPDATE graph_episode SET status = 'stale', updated_at = now() WHERE id IN ("
                " SELECT ged.graph_episode_id FROM graph_episode_document ged"
                " JOIN source_document d ON d.id = ged.document_id"
                " WHERE d.source = 'github' AND d.external_id = ANY(:ids))"
            ),
            {"ids": external_ids},
        )


async def link_orphans(workspace_id: str, container_id: str) -> None:
    """Attach comments whose parent arrived later (e.g. out-of-order backfill pages)."""
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE source_document c SET parent_id = p.id FROM source_document p"
                " WHERE c.container_id = :cid AND c.parent_id IS NULL"
                " AND c.metadata ? 'parent_number' AND p.container_id = :cid"
                " AND p.kind IN ('issue', 'pull_request')"
                " AND (p.metadata->>'number')::int = (c.metadata->>'parent_number')::int"
            ),
            {"cid": container_id},
        )


async def set_pr_approvals(
    workspace_id: str, container_id: str, number: int, approved_by: list[str]
) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE source_document SET metadata = metadata || CAST(:m AS jsonb)"
                " WHERE container_id = :cid AND kind = 'pull_request'"
                " AND (metadata->>'number')::int = :n"
            ),
            {"m": json.dumps({"approved_by": approved_by}), "cid": container_id, "n": number},
        )
