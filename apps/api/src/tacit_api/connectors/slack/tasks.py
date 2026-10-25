"""Celery tasks for the Slack connector. Each one is a thin wrapper over async code.

Rate limits: the SDK retries short Retry-After waits itself; longer ones raise and the
task retries later with that countdown instead of blocking a worker.
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
PREFIX = "tacit_api.connectors.slack.tasks."


def _retry_if_rate_limited(task: Task, exc: Exception) -> None:
    wait = rate_limit_wait(exc)
    if wait is None:
        raise exc
    raise task.retry(exc=exc, countdown=wait + random.randint(0, 15))


async def _client(workspace_id: str, team_id: str):
    inst = await sync.load_installation(workspace_id, team_id)
    return (inst, sync.client_for(inst)) if inst else (None, None)


@celery_app.task(name=PREFIX + "process_delivery", max_retries=5)
@run_async
async def process_delivery(delivery_id: str) -> str:
    return await handlers.process_delivery(delivery_id)


@celery_app.task(name=PREFIX + "sync_team", bind=True, max_retries=10)
def sync_team(self: Task, workspace_id: str, team_id: str) -> None:
    """Mirror the bot's channels, sync access, then start backfills for new channels."""

    @run_async
    async def run() -> list[str]:
        inst, client = await _client(workspace_id, team_id)
        if inst is None:
            return []
        pending = await sync.sync_channels(workspace_id, inst, client)
        await sync.sync_team_acl(workspace_id, team_id, client)
        return pending

    try:
        pending = run()
    except Exception as exc:
        _retry_if_rate_limited(self, exc)
    for cid in pending:
        backfill_channel.delay(workspace_id, cid)


@celery_app.task(name=PREFIX + "sync_acl", bind=True, max_retries=10)
def sync_acl(self: Task, workspace_id: str, team_id: str) -> None:
    @run_async
    async def run() -> None:
        inst, client = await _client(workspace_id, team_id)
        if inst is not None:
            await sync.sync_team_acl(workspace_id, team_id, client)

    try:
        run()
    except Exception as exc:
        _retry_if_rate_limited(self, exc)


@celery_app.task(name=PREFIX + "backfill_channel", bind=True, max_retries=20)
def backfill_channel(self: Task, workspace_id: str, container_id: str, reconcile: bool = False):
    """One history page, then re-enqueue for the next page."""

    @run_async
    async def run() -> bool | None:
        ch = await sync.load_channel(workspace_id, container_id)
        if ch is None:
            return None  # channel removed or disabled meanwhile
        inst, client = await _client(workspace_id, ch.team_id)
        if inst is None:
            return None
        await backfill.set_backfill_status(workspace_id, container_id, "running")
        return await backfill.backfill_page(workspace_id, ch, client, reconcile=reconcile)

    try:
        more = run()
    except Exception as exc:
        if rate_limit_wait(exc) is None:
            run_async(backfill.set_backfill_status)(workspace_id, container_id, "failed")
        _retry_if_rate_limited(self, exc)
    if more is None:
        return
    from ...search.tasks import index_workspace_task

    index_workspace_task.delay(workspace_id)
    if more:
        backfill_channel.delay(workspace_id, container_id, reconcile)
    else:
        run_async(backfill.set_backfill_status)(workspace_id, container_id, "done")


# --- Scheduled jobs -------------------------------------------------------------------


@run_async
async def _all_installations() -> list[tuple[str, str]]:
    async with worker_session() as s:
        rows = await s.execute(text("SELECT * FROM app.list_slack_installations()"))
        return [(r[0], r[1]) for r in rows]


@celery_app.task(name=PREFIX + "refresh_all_acls")
def refresh_all_acls() -> None:
    """Hourly: keeps access well inside the 6h staleness cutoff."""
    for workspace_id, team_id in _all_installations():
        sync_acl.delay(workspace_id, team_id)


@celery_app.task(name=PREFIX + "reconcile_all")
def reconcile_all() -> None:
    """Every 6h: re-list each channel from its watermark minus an overlap."""
    for workspace_id, team_id in _all_installations():

        @run_async
        async def channels(ws: str = workspace_id, team: str = team_id) -> list[str]:
            return [c.container_id for c in await sync.active_channels(ws, team)]

        for cid in channels():
            backfill_channel.delay(workspace_id, cid, True)


celery_app.conf.task_routes = {
    **(celery_app.conf.task_routes or {}),
    PREFIX + "backfill_channel": {"queue": "backfill"},
}
celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "slack-refresh-acls": {"task": PREFIX + "refresh_all_acls", "schedule": crontab(minute=13)},
    "slack-reconcile": {
        "task": PREFIX + "reconcile_all",
        "schedule": crontab(minute=37, hour="*/6"),
    },
}
