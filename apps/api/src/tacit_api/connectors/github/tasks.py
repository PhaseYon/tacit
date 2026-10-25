"""Celery tasks for the GitHub connector. Each one is a thin wrapper over async code.

Rate limits: githubkit retries short waits itself. Longer ones raise, and the task
retries later with a countdown instead of blocking a worker.
"""

import logging
import random
import time

from celery import Task
from celery.schedules import crontab
from sqlalchemy import text

from ...db import worker_session
from ...worker import celery_app, run_async
from . import backfill, handlers, sync
from .client import RateLimitErrors, installation_client

log = logging.getLogger(__name__)
PREFIX = "tacit_api.connectors.github.tasks."


def _retry_on_rate_limit(task: Task, exc: Exception) -> None:
    reset = getattr(getattr(exc, "response", None), "headers", {}).get("x-ratelimit-reset")
    wait = max(int(reset) - int(time.time()), 30) if reset else 60
    raise task.retry(exc=exc, countdown=wait + random.randint(0, 30))


async def _installation_for(workspace_id: str, container_id: str) -> int | None:
    async with worker_session(workspace_id) as s:
        return await s.scalar(
            text("SELECT installation_id FROM github_repository WHERE container_id = :c"),
            {"c": container_id},
        )


@celery_app.task(name=PREFIX + "process_delivery", max_retries=5)
@run_async
async def process_delivery(delivery_id: str) -> str:
    return await handlers.process_delivery(delivery_id)


@celery_app.task(name=PREFIX + "sync_installation", bind=True, max_retries=10)
def sync_installation(self: Task, workspace_id: str, installation_id: int) -> None:
    """Mirror repos, sync each repo's access, then start backfills for new repos."""

    @run_async
    async def run() -> list[str]:
        gh = installation_client(installation_id)
        await sync.sync_installation_repos(workspace_id, installation_id, gh)
        for repo in await sync.active_repos(workspace_id, installation_id):
            await sync.sync_repo_acl(workspace_id, repo, gh)
        async with worker_session(workspace_id) as s:
            return list(
                (
                    await s.execute(
                        text(
                            "SELECT container_id::text FROM github_repository"
                            " WHERE installation_id = :i AND backfill_status = 'pending'"
                        ),
                        {"i": installation_id},
                    )
                ).scalars()
            )

    try:
        pending = run()
    except RateLimitErrors as exc:
        _retry_on_rate_limit(self, exc)
    for cid in pending:
        backfill_repo.delay(workspace_id, cid, backfill.ORDER[0])


@celery_app.task(name=PREFIX + "sync_acl", bind=True, max_retries=10)
def sync_acl(self: Task, workspace_id: str, installation_id: int) -> None:
    @run_async
    async def run() -> None:
        gh = installation_client(installation_id)
        for repo in await sync.active_repos(workspace_id, installation_id):
            await sync.sync_repo_acl(workspace_id, repo, gh)

    try:
        run()
    except RateLimitErrors as exc:
        _retry_on_rate_limit(self, exc)


@celery_app.task(name=PREFIX + "backfill_repo", bind=True, max_retries=20)
def backfill_repo(self: Task, workspace_id: str, container_id: str, resource: str) -> None:
    """One page of one resource, then re-enqueue: the next page, or the next resource."""

    @run_async
    async def run() -> bool | None:
        repo = await backfill.load_repo(workspace_id, container_id)
        iid = await _installation_for(workspace_id, container_id)
        if repo is None or iid is None:
            return None  # repo was removed or disabled meanwhile
        if resource == backfill.ORDER[0]:
            await backfill.set_backfill_status(workspace_id, container_id, "running")
        return await backfill.backfill_page(workspace_id, repo, resource, installation_client(iid))

    try:
        more = run()
    except RateLimitErrors as exc:
        _retry_on_rate_limit(self, exc)
    except Exception:
        run_async(backfill.set_backfill_status)(workspace_id, container_id, "failed")
        raise
    if more is None:
        return
    if more:
        backfill_repo.delay(workspace_id, container_id, resource)
        return
    i = backfill.ORDER.index(resource)
    if i + 1 < len(backfill.ORDER):
        backfill_repo.delay(workspace_id, container_id, backfill.ORDER[i + 1])
    else:
        run_async(backfill.set_backfill_status)(workspace_id, container_id, "done")


@celery_app.task(name=PREFIX + "sync_pr_reviews", bind=True, max_retries=10)
def sync_pr_reviews(self: Task, workspace_id: str, container_id: str, number: int) -> None:
    @run_async
    async def run() -> None:
        repo = await backfill.load_repo(workspace_id, container_id)
        iid = await _installation_for(workspace_id, container_id)
        if repo and iid:
            await backfill.sync_pr_reviews(workspace_id, repo, number, installation_client(iid))

    try:
        run()
    except RateLimitErrors as exc:
        _retry_on_rate_limit(self, exc)


@celery_app.task(name=PREFIX + "sync_markdown", bind=True, max_retries=10)
def sync_markdown(self: Task, workspace_id: str, container_id: str) -> None:
    @run_async
    async def run() -> None:
        repo = await backfill.load_repo(workspace_id, container_id)
        iid = await _installation_for(workspace_id, container_id)
        if repo and iid:
            await backfill.sync_markdown(workspace_id, repo, installation_client(iid))

    try:
        run()
    except RateLimitErrors as exc:
        _retry_on_rate_limit(self, exc)


# --- Scheduled jobs -------------------------------------------------------------------


@run_async
async def _all_installations() -> list[tuple[str, int]]:
    async with worker_session() as s:
        rows = await s.execute(text("SELECT * FROM app.list_github_installations()"))
        return [(r[0], r[1]) for r in rows]


@celery_app.task(name=PREFIX + "refresh_all_acls")
def refresh_all_acls() -> None:
    """Hourly. Mostly 304s thanks to ETags; keeps access well inside the 6h cutoff."""
    for workspace_id, installation_id in _all_installations():
        sync_acl.delay(workspace_id, installation_id)


@celery_app.task(name=PREFIX + "reconcile_all")
def reconcile_all() -> None:
    """Every 6h: re-list each repo from its last cursor to catch missed webhooks."""
    for workspace_id, installation_id in _all_installations():

        @run_async
        async def repos(ws: str = workspace_id, iid: int = installation_id) -> list[str]:
            return [r.container_id for r in await sync.active_repos(ws, iid)]

        for cid in repos():
            backfill_repo.delay(workspace_id, cid, backfill.ORDER[0])


@celery_app.task(name=PREFIX + "purge_old_deliveries")
def purge_old_deliveries() -> None:
    @run_async
    async def run() -> None:
        async with worker_session() as s:
            await s.execute(
                text("DELETE FROM webhook_delivery WHERE received_at < now() - interval '14 days'")
            )

    run()


celery_app.conf.beat_schedule = {
    "github-refresh-acls": {"task": PREFIX + "refresh_all_acls", "schedule": crontab(minute=7)},
    "github-reconcile": {
        "task": PREFIX + "reconcile_all",
        "schedule": crontab(minute=23, hour="*/6"),
    },
    "github-purge-deliveries": {
        "task": PREFIX + "purge_old_deliveries",
        "schedule": crontab(minute=41, hour=3),
    },
}
