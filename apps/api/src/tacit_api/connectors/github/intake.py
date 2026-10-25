"""Webhook intake: store each delivery once, then hand it to a worker.

GitHub times out after 10 s and never redelivers on its own, so this path does no
GitHub calls and no heavy DB work.
"""

import json
from typing import Any

from sqlalchemy import text

from ...db import worker_session


async def record_delivery(delivery_id: str, event: str, payload: dict[str, Any]) -> bool:
    """Insert the delivery. Returns False if we've already seen this delivery id."""
    installation_id = (payload.get("installation") or {}).get("id")
    async with worker_session() as s:
        inserted = await s.scalar(
            text(
                "INSERT INTO webhook_delivery"
                " (delivery_id, event, action, installation_id, payload)"
                " VALUES (:id, :event, :action, :inst, CAST(:payload AS jsonb))"
                " ON CONFLICT (delivery_id) DO NOTHING RETURNING true"
            ),
            {
                "id": delivery_id,
                "event": event,
                "action": payload.get("action"),
                "inst": installation_id,
                "payload": json.dumps(payload),
            },
        )
    return bool(inserted)


def enqueue_delivery(delivery_id: str) -> None:
    from .tasks import process_delivery

    process_delivery.delay(delivery_id)
