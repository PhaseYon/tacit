"""Celery tasks for the Notion connector. Each one is a thin wrapper over async code.

Rate limits: the SDK retries 429s itself (Retry-After, capped at 60 s); what survives
raises, and the task retries later with that countdown instead of blocking a worker.
"""

import logging
import random

from celery import Task
from celery.schedules import crontab
from sqlalchemy import text

from ...db import worker_session
from ...worker import celery_app, run_async
from . import backfill, handlers, sync
from .client import rate_limit_wait

log = logging.getLogger(__name__)
PREFIX = "tacit_api.connectors.notion.tasks."


def _retry_if_rate_limited(task: Task, exc: Exception) -> None:
    wait = rate_limit_wait(exc)
    if wait is None:
        raise exc
    raise task.retry(exc=exc, countdown=wait + random.randint(0, 15))


@celery_app.task(name=PREFIX + "process_delivery", bind=True, max_retries=5)
def process_delivery(self: Task, delivery_id: str) -> str:
    try:
        return run_async(handlers.process_delivery)(delivery_id)
    except Exception as exc:
        _retry_if_rate_limited(self, exc)
        raise


@celery_app.task(name=PREFIX + "sync_connection", bind=True, max_retries=20)
def sync_connection(
    self: Task,
    workspace_id: str,
    connection_id: str,
    mode: str = "incremental",
    continuation: bool = False,
) -> None:
    """One search page, then re-enqueue for the next page."""

    @run_async
    async def run() -> bool | None:
        async def page(conn, client):
            return await backfill.run_page(conn, client, mode=mode, continuation=continuation)

        return await sync.with_connection(workspace_id, connection_id, page)

    try:
        more = run()
    except Exception as exc:
        _retry_if_rate_limited(self, exc)
        raise
    if more is None:
        return  # no live connection, or another run is in flight
    from ...search.tasks import index_workspace_task

    index_workspace_task.delay(workspace_id)
    if more:
        sync_connection.delay(workspace_id, connection_id, mode, True)


# --- Scheduled jobs -------------------------------------------------------------------


@run_async
async def _all_connections() -> list[tuple[str, str]]:
    async with worker_session() as s:
        rows = await s.execute(text("SELECT * FROM app.list_notion_connections()"))
        return [(r[0], str(r[1])) for r in rows]


@celery_app.task(name=PREFIX + "refresh_all_acls")
def refresh_all_acls() -> None:
    """Hourly: keeps access well inside the 6h staleness cutoff (no Notion calls)."""
    for workspace_id, connection_id in _all_connections():
        run_async(sync.sync_acl)(workspace_id, connection_id)


@celery_app.task(name=PREFIX + "reconcile_all")
def reconcile_all(mode: str = "incremental") -> None:
    for workspace_id, connection_id in _all_connections():
        sync_connection.delay(workspace_id, connection_id, mode)


celery_app.conf.task_routes = {
    **(celery_app.conf.task_routes or {}),
    PREFIX + "sync_connection": {"queue": "backfill"},
}
celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "notion-refresh-acls": {"task": PREFIX + "refresh_all_acls", "schedule": crontab(minute=7)},
    "notion-reconcile": {
        "task": PREFIX + "reconcile_all",
        "schedule": crontab(minute=23, hour="*/2"),
    },
    "notion-full-sweep": {
        "task": PREFIX + "reconcile_all",
        "schedule": crontab(minute=41, hour=4),
        "args": ("full",),
    },
}
