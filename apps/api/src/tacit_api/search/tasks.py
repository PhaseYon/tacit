"""Indexing tasks. Safe to run concurrently: rows are claimed with SKIP LOCKED."""

from celery.schedules import crontab
from sqlalchemy import text

from ..db import worker_session
from ..worker import celery_app, run_async
from .indexer import index_workspace

PREFIX = "tacit_api.search.tasks."


@celery_app.task(name=PREFIX + "index_workspace", queue="index")
@run_async
async def index_workspace_task(workspace_id: str) -> int:
    return (await index_workspace(workspace_id)).documents


@celery_app.task(name=PREFIX + "index_all")
def index_all() -> None:
    """Every minute: catch anything an event-driven trigger missed."""

    @run_async
    async def pending() -> list[str]:
        async with worker_session() as s:
            return list((await s.execute(text("SELECT app.workspaces_pending_index()"))).scalars())

    for workspace_id in pending():
        index_workspace_task.delay(workspace_id)


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "search-index-all": {"task": PREFIX + "index_all", "schedule": crontab()},
}
