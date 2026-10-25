"""Celery tasks for the graph layer. Only scheduled when GRAPH_ENABLED=true."""

import logging

import redis
from celery.schedules import crontab
from sqlalchemy import text

from ..config import get_settings
from ..db import worker_session
from ..llm import LLMNotConfigured, get_provider
from ..worker import celery_app, run_async
from . import pipeline
from .client import build_graphiti, group_for

log = logging.getLogger(__name__)
PREFIX = "tacit_api.graph.tasks."


@celery_app.task(name=PREFIX + "graph_scan_all")
def graph_scan_all() -> None:
    if not get_settings().graph_enabled:
        return

    @run_async
    async def run() -> list[tuple[str, str]]:
        async with worker_session() as s:
            workspaces = list(
                (await s.execute(text("SELECT app.workspaces_graph_enabled()"))).scalars()
            )
        work = []
        provider = get_provider()
        for ws in workspaces:
            await pipeline.scan(ws, provider)
            async with worker_session(ws) as s:
                cids = (
                    await s.execute(
                        text(
                            "SELECT DISTINCT container_id::text FROM graph_episode"
                            " WHERE status IN ('pending', 'stale')"
                        )
                    )
                ).scalars()
                work += [(ws, c) for c in cids]
        return work

    try:
        work = run()
    except LLMNotConfigured as exc:
        log.warning("Graph enabled but no LLM configured: %s", exc)
        return
    for ws, cid in work:
        graph_process_container.delay(ws, cid)


@celery_app.task(name=PREFIX + "graph_process_container", queue="graph")
def graph_process_container(workspace_id: str, container_id: str) -> None:
    """Graphiti requires episodes in one group to be added sequentially: one lock per group."""
    r = redis.Redis.from_url(get_settings().redis_url)
    lock = r.lock(f"graph:{group_for(container_id)}", timeout=3600, blocking=False)
    if not lock.acquire(blocking=False):
        return  # another worker is on it; the next scan re-queues leftovers

    @run_async
    async def run() -> dict:
        g = build_graphiti()
        try:
            return await pipeline.process_container(workspace_id, container_id, g, get_provider())
        finally:
            await g.close()

    try:
        log.info("Graph %s/%s: %s", workspace_id, container_id, run())
    finally:
        lock.release()


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "graph-scan": {"task": PREFIX + "graph_scan_all", "schedule": crontab(minute="*/5")},
}
