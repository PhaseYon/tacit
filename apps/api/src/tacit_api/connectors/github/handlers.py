"""Turn stored webhook deliveries into connector work."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from ...db import worker_session

log = logging.getLogger(__name__)

# (workspace_id, payload) -> outcome ("done" or "ignored")
Handler = Callable[[str, dict[str, Any]], Awaitable[str]]
HANDLERS: dict[str, Handler] = {}


def on(*events: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        for e in events:
            HANDLERS[e] = fn
        return fn

    return register


async def process_delivery(delivery_id: str) -> str:
    async with worker_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT event, installation_id, payload, status FROM webhook_delivery"
                    " WHERE delivery_id = :id"
                ),
                {"id": delivery_id},
            )
        ).one_or_none()
        if row is None or row.status == "done":
            return "skipped"
        workspace_id = (
            await s.scalar(
                text("SELECT app.workspace_for_installation(:i)"), {"i": row.installation_id}
            )
            if row.installation_id
            else None
        )

    handler = HANDLERS.get(row.event)
    try:
        if handler is None or workspace_id is None:
            # Unknown event, or an installation we haven't linked to a workspace yet
            # (the install flow will sync it once it's linked).
            outcome = "ignored"
        else:
            outcome = await handler(workspace_id, row.payload)
    except Exception as exc:
        await _finish(delivery_id, "failed", error=repr(exc)[:2000])
        raise
    await _finish(delivery_id, outcome)
    return outcome


async def _finish(delivery_id: str, status: str, error: str | None = None) -> None:
    async with worker_session() as s:
        await s.execute(
            text(
                "UPDATE webhook_delivery SET status = :st, error = :err,"
                " attempts = attempts + 1, processed_at = now() WHERE delivery_id = :id"
            ),
            {"st": status, "err": error, "id": delivery_id},
        )
