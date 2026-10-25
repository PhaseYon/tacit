"""Celery tasks for the GitHub connector. Each one wraps an async handler."""

from ...worker import celery_app, run_async
from . import handlers


@celery_app.task(name="tacit_api.connectors.github.tasks.process_delivery", max_retries=5)
@run_async
async def process_delivery(delivery_id: str) -> str:
    return await handlers.process_delivery(delivery_id)
