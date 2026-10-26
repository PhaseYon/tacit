"""Webhook intake and event handlers for Notion.

Notion events are signals, not content: handlers never trust the payload's data. They
re-fetch with each affected connection's own token, so a replayed or forged-but-signed
event can at most trigger a refetch. Lost access (404 / restricted) hides the page.
"""

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import text

from ...db import worker_session
from . import backfill, ingest
from .sync import recompute_trees, with_connection

log = logging.getLogger(__name__)

REFETCH = {
    "page.created",
    "page.content_updated",
    "page.properties_updated",
    "page.moved",
    "page.undeleted",
}
RECONCILE_PREFIXES = ("database.", "data_source.")


def delivery_id(event_id: str) -> str:
    return f"notion:{event_id}"


async def record_delivery(payload: dict[str, Any]) -> str | None:
    """Store a signed event. Returns the delivery id, or None if already seen."""
    did = delivery_id(str(payload["id"]))
    async with worker_session() as s:
        inserted = await s.scalar(
            text(
                "INSERT INTO webhook_delivery (delivery_id, source, event, payload)"
                " VALUES (:id, 'notion', :event, CAST(:payload AS jsonb))"
                " ON CONFLICT (delivery_id) DO NOTHING RETURNING true"
            ),
            {"id": did, "event": payload.get("type") or "unknown", "payload": json.dumps(payload)},
        )
    return did if inserted else None


async def record_verification(payload: dict[str, Any]) -> None:
    """Notion's one-time, unsigned verification request. Kept in the worker-only intake
    table (never logged) so the operator can copy the token into Notion and the env."""
    token = str(payload.get("verification_token") or "")
    did = "notion:verification:" + hashlib.sha256(token.encode()).hexdigest()[:16]
    async with worker_session() as s:
        await s.execute(
            text(
                "INSERT INTO webhook_delivery (delivery_id, source, event, status, payload)"
                " VALUES (:id, 'notion', 'verification', 'ignored', CAST(:payload AS jsonb))"
                " ON CONFLICT (delivery_id) DO NOTHING"
            ),
            {"id": did, "payload": json.dumps({"verification_token": token})},
        )


def enqueue_delivery(did: str) -> None:
    from .tasks import process_delivery

    process_delivery.delay(did)


async def _targets(event: dict[str, Any], page_id: str | None) -> list[tuple[str, str]]:
    """(workspace id, connection id) of live connections in the event's Notion workspace
    that Notion says can see the entity, or that already hold the page."""
    async with worker_session() as s:
        conns = (
            await s.execute(
                text("SELECT * FROM app.notion_connections_for(:nws)"),
                {"nws": str(event.get("workspace_id") or "")},
            )
        ).all()
    accessible = {str(a.get("id")) for a in event.get("accessible_by") or []}
    out = []
    for ws, cid, bot_id, _person in conns:
        # No accessible_by: ask every connection (each refetch uses its own token, and a
        # 404 just hides that connection's copy).
        if (
            not accessible
            or bot_id in accessible
            or (page_id and await ingest.container_of(ws, str(cid), page_id))
        ):
            out.append((ws, str(cid)))
    return out


async def process_delivery(did: str) -> str:
    async with worker_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT payload, status FROM webhook_delivery"
                    " WHERE delivery_id = :id AND source = 'notion'"
                ),
                {"id": did},
            )
        ).one_or_none()
    if row is None or row.status == "done":
        return "skipped"
    event = row.payload or {}
    try:
        outcome, workspaces = await handle(event)
    except Exception as exc:
        await _finish(did, "failed", repr(exc)[:2000])
        raise
    await _finish(did, outcome)
    if outcome == "done":
        from ...search.tasks import index_workspace_task

        for ws in sorted(workspaces):
            index_workspace_task.delay(ws)
    return outcome


async def handle(event: dict[str, Any]) -> tuple[str, set[str]]:
    etype = str(event.get("type") or "")
    entity = event.get("entity") or {}
    entity_id = str(entity.get("id") or "") or None
    if etype in REFETCH and entity.get("type") == "page" and entity_id:
        targets = await _targets(event, entity_id)
        for ws, cid in targets:

            async def refetch(conn, client, page_id: str = entity_id):
                return await backfill.sync_page(conn, client, page_id)

            await with_connection(ws, cid, refetch)
        return ("done" if targets else "ignored"), {ws for ws, _ in targets}
    if etype == "page.deleted" and entity_id:
        targets = await _targets(event, entity_id)
        for ws, cid in targets:
            await ingest.forget_pages(ws, cid, [entity_id])
            await recompute_trees(ws, cid)
        return ("done" if targets else "ignored"), {ws for ws, _ in targets}
    if etype.startswith(RECONCILE_PREFIXES):
        targets = await _targets(event, None)
        from .tasks import sync_connection

        for ws, cid in targets:
            sync_connection.delay(ws, cid)
        return ("done" if targets else "ignored"), set()
    return "ignored", set()  # comments, locks, unknown types


async def _finish(did: str, status: str, error: str | None = None) -> None:
    async with worker_session() as s:
        await s.execute(
            text(
                "UPDATE webhook_delivery SET status = :st, error = :err,"
                " attempts = attempts + 1, processed_at = now() WHERE delivery_id = :id"
            ),
            {"st": status, "err": error, "id": did},
        )
