"""Celery app. Run with: uv run celery -A tacit_api.worker worker -B -Q realtime,backfill,index

Tasks are thin sync wrappers around async handlers (see `run_async`). Every task is
safe to re-run: writes are upserts, and late acks mean a crashed worker's task is
redelivered rather than lost.
"""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps

from celery import Celery

from .config import get_settings

celery_app = Celery("tacit", broker=get_settings().redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="realtime",
    # Webhooks and ACL changes go to "realtime" so a big backfill can't delay them.
    task_routes={"tacit_api.connectors.github.tasks.backfill_repo": {"queue": "backfill"}},
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    broker_connection_retry_on_startup=True,
    include=["tacit_api.connectors.github.tasks", "tacit_api.search.tasks"],
)


def run_async[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, R]:
    """Run an async handler in a fresh event loop (Celery workers are sync)."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        async def main() -> R:
            from .db import dispose_engine

            try:
                return await fn(*args, **kwargs)
            finally:
                await dispose_engine()

        return asyncio.run(main())

    return wrapper
