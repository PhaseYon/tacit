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
    if outcome == "done" and row.event in CONTENT_EVENTS:
        _index_soon(workspace_id)
    return outcome


CONTENT_EVENTS = {
    "issues",
    "pull_request",
    "issue_comment",
    "pull_request_review",
    "pull_request_review_comment",
}


def _index_soon(workspace_id: str) -> None:
    from ...search.tasks import index_workspace_task

    index_workspace_task.delay(workspace_id)


async def _finish(delivery_id: str, status: str, error: str | None = None) -> None:
    async with worker_session() as s:
        await s.execute(
            text(
                "UPDATE webhook_delivery SET status = :st, error = :err,"
                " attempts = attempts + 1, processed_at = now() WHERE delivery_id = :id"
            ),
            {"st": status, "err": error, "id": delivery_id},
        )


# --- Event handlers ------------------------------------------------------------------
# Content events carry the full object, so they're normalized straight from the payload
# with no API call. Anything that changes access is handled before anything else.


async def _container_for(workspace_id: str, repo: dict[str, Any] | None) -> str | None:
    if not repo:
        return None
    async with worker_session(workspace_id) as s:
        return await s.scalar(
            text(
                "SELECT id::text FROM source_container WHERE source = 'github'"
                " AND external_id = :ext AND is_active"
            ),
            {"ext": str(repo["id"])},
        )


def _tasks():
    from . import tasks

    return tasks


@on("issues", "pull_request")
async def handle_issue_or_pr(workspace_id: str, payload: dict[str, Any]) -> str:
    from .ingest import tombstone, upsert_documents
    from .normalize import normalize_issue

    obj = payload.get("issue") or payload.get("pull_request")
    if obj is None:
        return "ignored"
    if payload.get("action") in ("deleted", "transferred"):
        await tombstone(workspace_id, [obj["node_id"]])
        return "done"
    cid = await _container_for(workspace_id, payload.get("repository"))
    if cid is None:
        return "ignored"
    await upsert_documents(workspace_id, cid, [normalize_issue(obj, payload["repository"])])
    return "done"


@on("issue_comment")
async def handle_issue_comment(workspace_id: str, payload: dict[str, Any]) -> str:
    from .ingest import tombstone, upsert_documents
    from .normalize import normalize_issue_comment

    comment = payload["comment"]
    if payload.get("action") == "deleted":
        await tombstone(workspace_id, [comment["node_id"]])
        return "done"
    cid = await _container_for(workspace_id, payload.get("repository"))
    if cid is None:
        return "ignored"
    await upsert_documents(
        workspace_id, cid, [normalize_issue_comment(comment, payload["repository"])]
    )
    return "done"


@on("pull_request_review_comment")
async def handle_review_comment(workspace_id: str, payload: dict[str, Any]) -> str:
    from .ingest import tombstone, upsert_documents
    from .normalize import normalize_pr_review_comment

    comment = payload["comment"]
    if payload.get("action") == "deleted":
        await tombstone(workspace_id, [comment["node_id"]])
        return "done"
    cid = await _container_for(workspace_id, payload.get("repository"))
    if cid is None:
        return "ignored"
    await upsert_documents(
        workspace_id, cid, [normalize_pr_review_comment(comment, payload["repository"])]
    )
    return "done"


@on("pull_request_review")
async def handle_review(workspace_id: str, payload: dict[str, Any]) -> str:
    from .ingest import upsert_documents
    from .normalize import normalize_pr_review

    cid = await _container_for(workspace_id, payload.get("repository"))
    if cid is None:
        return "ignored"
    number = payload["pull_request"]["number"]
    await upsert_documents(
        workspace_id, cid, [normalize_pr_review(payload["review"], number, payload["repository"])]
    )
    # Approvals depend on every review, so recompute from the full list.
    _tasks().sync_pr_reviews.delay(workspace_id, cid, number)
    return "done"


@on("push")
async def handle_push(workspace_id: str, payload: dict[str, Any]) -> str:
    repo = payload.get("repository") or {}
    if payload.get("ref") != f"refs/heads/{repo.get('default_branch')}":
        return "ignored"
    paths = {
        p
        for c in payload.get("commits") or []
        for key in ("added", "modified", "removed")
        for p in c.get(key) or []
    }
    if not any(p.lower().endswith(".md") for p in paths):
        return "ignored"
    cid = await _container_for(workspace_id, repo)
    if cid is None:
        return "ignored"
    # Repos can have custom globs, so the markdown sync decides what matches; this
    # check only skips pushes that touch no markdown at all.
    _tasks().sync_markdown.delay(workspace_id, cid)
    return "done"


@on("installation")
async def handle_installation(workspace_id: str, payload: dict[str, Any]) -> str:
    from .sync import deactivate_containers

    action = payload.get("action")
    iid = payload["installation"]["id"]
    if action in ("deleted", "suspend"):
        await deactivate_containers(workspace_id, None)
        column = "deleted_at" if action == "deleted" else "suspended_at"
        async with worker_session(workspace_id) as s:
            await s.execute(
                text(
                    f"UPDATE github_installation SET {column} = now(), updated_at = now()"
                    " WHERE installation_id = :iid"
                ),
                {"iid": iid},
            )
        return "done"
    if action in ("unsuspend", "new_permissions_accepted", "created"):
        async with worker_session(workspace_id) as s:
            await s.execute(
                text(
                    "UPDATE github_installation SET suspended_at = NULL, updated_at = now()"
                    " WHERE installation_id = :iid"
                ),
                {"iid": iid},
            )
        _tasks().sync_installation.delay(workspace_id, iid)
        return "done"
    return "ignored"


@on("installation_repositories")
async def handle_installation_repositories(workspace_id: str, payload: dict[str, Any]) -> str:
    from .sync import deactivate_containers

    removed = [str(r["id"]) for r in payload.get("repositories_removed") or []]
    if removed:
        await deactivate_containers(workspace_id, removed)  # access goes first, right now
    _tasks().sync_installation.delay(workspace_id, payload["installation"]["id"])
    return "done"


@on("repository")
async def handle_repository(workspace_id: str, payload: dict[str, Any]) -> str:
    from .sync import deactivate_containers

    repo = payload["repository"]
    if payload.get("action") in ("deleted", "transferred"):
        await deactivate_containers(workspace_id, [str(repo["id"])])
        return "done"
    if payload.get("action") == "privatized":
        # Narrow access now; don't wait for the queued sync (fail closed).
        async with worker_session(workspace_id) as s:
            await s.execute(
                text(
                    "UPDATE source_container SET visibility = 'private', updated_at = now()"
                    " WHERE source = 'github' AND external_id = :ext"
                ),
                {"ext": str(repo["id"])},
            )
    # privatized / publicized / renamed / archived / edited: refresh repo + its access.
    _tasks().sync_installation.delay(workspace_id, payload["installation"]["id"])
    return "done"


@on("member", "membership", "team", "organization")
async def handle_access_change(workspace_id: str, payload: dict[str, Any]) -> str:
    _tasks().sync_acl.delay(workspace_id, payload["installation"]["id"])
    return "done"
