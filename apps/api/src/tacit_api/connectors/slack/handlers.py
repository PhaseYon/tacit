"""Webhook intake and Events API handlers for Slack.

Intake stores each event once (dedupe on event_id) and hands it to a worker; Slack
gives us 3 seconds and retries on its own, so the request path makes no Slack calls.
Message events carry the full message, so they're normalized with no API call.
Anything that narrows access runs before anything else.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from ...db import worker_session
from . import normalize as n
from .ingest import tombstone, upsert_documents
from .sync import deactivate_channels, load_directory, revoke_installation, revoke_member_access

log = logging.getLogger(__name__)

# (workspace_id, team_id, event) -> "done" | "ignored"
Handler = Callable[[str, str, dict[str, Any]], Awaitable[str]]
HANDLERS: dict[str, Handler] = {}


def on(*events: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        for e in events:
            HANDLERS[e] = fn
        return fn

    return register


def delivery_id(event_id: str) -> str:
    return f"slack:{event_id}"


async def record_delivery(envelope: dict[str, Any]) -> str | None:
    """Store an event_callback. Returns the delivery id, or None if already seen."""
    did = delivery_id(envelope["event_id"])
    event = envelope.get("event") or {}
    async with worker_session() as s:
        inserted = await s.scalar(
            text(
                "INSERT INTO webhook_delivery (delivery_id, source, event, action, team_id,"
                " payload) VALUES (:id, 'slack', :event, :action, :team, CAST(:payload AS jsonb))"
                " ON CONFLICT (delivery_id) DO NOTHING RETURNING true"
            ),
            {
                "id": did,
                "event": event.get("type") or "unknown",
                "action": event.get("subtype"),
                "team": envelope.get("team_id"),
                "payload": json.dumps(envelope),
            },
        )
    return did if inserted else None


def enqueue_delivery(did: str) -> None:
    from .tasks import process_delivery

    process_delivery.delay(did)


async def process_delivery(did: str) -> str:
    async with worker_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT team_id, payload, status FROM webhook_delivery"
                    " WHERE delivery_id = :id AND source = 'slack'"
                ),
                {"id": did},
            )
        ).one_or_none()
        if row is None or row.status == "done":
            return "skipped"
        workspace_id = (
            await s.scalar(text("SELECT app.workspace_for_slack_team(:t)"), {"t": row.team_id})
            if row.team_id
            else None
        )
    event = (row.payload or {}).get("event") or {}
    handler = HANDLERS.get(event.get("type", ""))
    try:
        if handler is None or workspace_id is None:
            outcome = "ignored"  # unknown event, or a team not linked to a workspace
        else:
            outcome = await handler(workspace_id, row.team_id, event)
    except Exception as exc:
        await _finish(did, "failed", repr(exc)[:2000])
        raise
    await _finish(did, outcome)
    if outcome == "done" and event.get("type") == "message":
        from ...search.tasks import index_workspace_task

        index_workspace_task.delay(workspace_id)
    return outcome


async def _finish(did: str, status: str, error: str | None = None) -> None:
    async with worker_session() as s:
        await s.execute(
            text(
                "UPDATE webhook_delivery SET status = :st, error = :err,"
                " attempts = attempts + 1, processed_at = now() WHERE delivery_id = :id"
            ),
            {"st": status, "err": error, "id": did},
        )


def _tasks():
    from . import tasks

    return tasks


async def _channel(
    workspace_id: str, team_id: str, channel_id: str | None
) -> tuple[str, n.Channel] | None:
    """(container id, channel) for a synced channel (active, sync on), or None."""
    if not channel_id:
        return None
    async with worker_session(workspace_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT c.id::text, sc.name, i.team_domain FROM source_container c"
                    " JOIN slack_channel sc ON sc.container_id = c.id"
                    " JOIN slack_installation i ON i.team_id = sc.team_id"
                    " WHERE c.source = 'slack' AND c.external_id = :ext AND c.is_active"
                    " AND sc.sync_enabled"
                ),
                {"ext": f"{team_id}:{channel_id}"},
            )
        ).one_or_none()
    if row is None:
        return None
    return row[0], n.Channel(team_id, channel_id, row[1], row[2])


# --- Messages -------------------------------------------------------------------------


@on("message")
async def handle_message(workspace_id: str, team_id: str, event: dict[str, Any]) -> str:
    found = await _channel(workspace_id, team_id, event.get("channel"))
    if found is None:
        return "ignored"
    container_id, ch = found
    subtype = event.get("subtype")
    if subtype == "message_deleted":
        await tombstone(
            workspace_id, [ch.external_id(event["deleted_ts"])], n.parse_ts(event.get("event_ts"))
        )
        return "done"
    if subtype == "message_changed":
        msg = event.get("message") or {}
        if msg.get("subtype") == "tombstone":  # a thread root deleted, replies kept
            await tombstone(
                workspace_id, [ch.external_id(msg["ts"])], n.parse_ts(event.get("event_ts"))
            )
            return "done"
    else:
        msg = event
    directory = await load_directory(workspace_id, team_id)
    doc = n.normalize_message(msg, ch, directory)
    if doc is None:
        return "ignored"
    await upsert_documents(workspace_id, container_id, [doc])
    return "done"


# --- Channels and membership ----------------------------------------------------------


@on("member_left_channel")
async def handle_member_left(workspace_id: str, team_id: str, event: dict[str, Any]) -> str:
    # Narrow first, with no API call; the resync below restores anything still allowed
    # (e.g. a full member leaving a public channel can still read it in Slack).
    await revoke_member_access(workspace_id, team_id, event["channel"], event["user"])
    _tasks().sync_acl.delay(workspace_id, team_id)
    return "done"


@on("channel_left", "group_left", "channel_deleted", "group_deleted")
async def handle_channel_gone(workspace_id: str, team_id: str, event: dict[str, Any]) -> str:
    await deactivate_channels(workspace_id, team_id, [event["channel"]])
    return "done"


@on("member_joined_channel")
async def handle_member_joined(workspace_id: str, team_id: str, event: dict[str, Any]) -> str:
    # The bot itself joining means a new channel to sync; anyone else changes the ACL.
    _tasks().sync_team.delay(workspace_id, team_id)
    return "done"


@on(
    "channel_rename",
    "group_rename",
    "channel_archive",
    "group_archive",
    "channel_unarchive",
    "group_unarchive",
)
async def handle_channel_change(workspace_id: str, team_id: str, event: dict[str, Any]) -> str:
    _tasks().sync_team.delay(workspace_id, team_id)
    return "done"


@on("team_join", "user_change")
async def handle_user_change(workspace_id: str, team_id: str, event: dict[str, Any]) -> str:
    user = event.get("user") or {}
    if user.get("deleted"):
        # Deactivated in Slack: drop every access row it granted right now.
        async with worker_session(workspace_id) as s:
            await s.execute(
                text("DELETE FROM container_access WHERE via_external_id = :via"),
                {"via": f"{team_id}:{user.get('id')}"},
            )
    _tasks().sync_acl.delay(workspace_id, team_id)
    return "done"


@on("app_uninstalled", "tokens_revoked")
async def handle_uninstall(workspace_id: str, team_id: str, event: dict[str, Any]) -> str:
    if event.get("type") == "tokens_revoked" and not (event.get("tokens") or {}).get("bot"):
        return "ignored"  # only user tokens revoked; we don't hold any
    await revoke_installation(workspace_id, team_id)
    return "done"
