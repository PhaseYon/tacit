"""Backfill and reconcile: fetch repo history one page at a time, resumably.

Each call to `backfill_page` fetches one page, upserts it, and saves a cursor, so a
crash resumes where it stopped. The same code reconciles later: it re-lists from the
last `since` minus an overlap, which catches anything webhooks missed.
"""

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from githubkit import GitHub
from sqlalchemy import text

from ...db import worker_session
from . import normalize as n
from .api import get_all, get_json, get_page
from .ingest import link_orphans, set_pr_approvals, tombstone, upsert_documents

log = logging.getLogger(__name__)

# Issues/PRs first so comments can find their parents.
ORDER = ["issues", "issue_comments", "review_comments", "docs"]
OVERLAP = timedelta(minutes=10)


@dataclass(frozen=True)
class Repo:
    container_id: str
    repo_id: int
    full_name: str
    html_url: str
    default_branch: str
    doc_globs: list[str] | None

    def as_json(self) -> dict[str, Any]:
        """Minimal repo dict the normalizers need."""
        return {
            "id": self.repo_id,
            "full_name": self.full_name,
            "html_url": self.html_url,
            "default_branch": self.default_branch,
        }


async def load_repo(workspace_id: str, container_id: str) -> Repo | None:
    async with worker_session(workspace_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT g.repo_id, g.full_name, c.url, g.default_branch, g.doc_globs"
                    " FROM github_repository g JOIN source_container c ON c.id = g.container_id"
                    " WHERE g.container_id = :cid AND c.is_active AND g.sync_enabled"
                ),
                {"cid": container_id},
            )
        ).one_or_none()
    if row is None:
        return None
    return Repo(container_id, row[0], row[1], row[2], row[3], row[4])


async def _cursor(workspace_id: str, container_id: str, resource: str):
    async with worker_session(workspace_id) as s:
        return (
            await s.execute(
                text(
                    "SELECT since, next_url FROM github_sync_cursor"
                    " WHERE container_id = :cid AND resource = :r"
                ),
                {"cid": container_id, "r": resource},
            )
        ).one_or_none()


async def _save_cursor(
    workspace_id: str,
    container_id: str,
    resource: str,
    *,
    next_url: str | None,
    since: datetime | None = None,
) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO github_sync_cursor (container_id, workspace_id, resource, since,"
                " next_url) VALUES (:cid, :ws, :r, :since, :next)"
                " ON CONFLICT (container_id, resource) DO UPDATE SET next_url = excluded.next_url,"
                " since = COALESCE(excluded.since, github_sync_cursor.since), updated_at = now()"
            ),
            {
                "cid": container_id,
                "ws": workspace_id,
                "r": resource,
                "since": since,
                "next": next_url,
            },
        )


async def set_backfill_status(workspace_id: str, container_id: str, status: str) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE github_repository SET backfill_status = :st,"
                " last_reconciled_at = CASE WHEN :st = 'done' THEN now()"
                " ELSE last_reconciled_at END WHERE container_id = :cid"
            ),
            {"st": status, "cid": container_id},
        )


def _list_url(repo: Repo, resource: str) -> str:
    return {
        "issues": f"/repos/{repo.full_name}/issues",
        "issue_comments": f"/repos/{repo.full_name}/issues/comments",
        "review_comments": f"/repos/{repo.full_name}/pulls/comments",
    }[resource]


async def backfill_page(workspace_id: str, repo: Repo, resource: str, gh: GitHub) -> bool:
    """Process one page. Returns True if there are more pages for this resource."""
    if resource == "docs":
        await sync_markdown(workspace_id, repo, gh)
        return False

    cursor = await _cursor(workspace_id, repo.container_id, resource)
    since: datetime | None = cursor.since if cursor else None
    if cursor and cursor.next_url:
        items, next_url = await get_page(gh, cursor.next_url)
    else:
        params: dict[str, Any] = {"sort": "updated", "direction": "asc", "per_page": 100}
        if resource == "issues":
            params["state"] = "all"
        if since:
            params["since"] = (since - OVERLAP).isoformat()
        items, next_url = await get_page(gh, _list_url(repo, resource), params)

    repo_json = repo.as_json()
    if resource == "issues":
        docs = [n.normalize_issue(i, repo_json) for i in items]
        # There's no repo-wide reviews endpoint: fetch reviews only for PRs that changed.
        changed_prs = [
            i["number"]
            for i in items
            if "pull_request" in i and (since is None or n.parse_ts(i["updated_at"]) > since)
        ]
    elif resource == "issue_comments":
        docs = [n.normalize_issue_comment(c, repo_json) for c in items]
        changed_prs = []
    else:
        docs = [n.normalize_pr_review_comment(c, repo_json) for c in items]
        changed_prs = []
    await upsert_documents(workspace_id, repo.container_id, docs)
    for number in changed_prs:
        await sync_pr_reviews(workspace_id, repo, number, gh)

    newest = max(
        (n.parse_ts(i.get("updated_at")) for i in items if i.get("updated_at")), default=None
    )
    if next_url:
        await _save_cursor(workspace_id, repo.container_id, resource, next_url=next_url)
        return True
    # Finished this resource: the next run lists from the newest item seen.
    await _save_cursor(
        workspace_id,
        repo.container_id,
        resource,
        next_url=None,
        since=max(filter(None, [since, newest]), default=None),
    )
    if resource != "issues":
        await link_orphans(workspace_id, repo.container_id)
    return False


async def sync_pr_reviews(workspace_id: str, repo: Repo, number: int, gh: GitHub) -> None:
    """Reviews with text become documents; every review feeds the PR's approved_by list."""
    reviews = await get_all(
        gh, f"/repos/{repo.full_name}/pulls/{number}/reviews", {"per_page": 100}
    )
    repo_json = repo.as_json()
    await upsert_documents(
        workspace_id,
        repo.container_id,
        [n.normalize_pr_review(r, number, repo_json) for r in reviews],
    )
    await set_pr_approvals(workspace_id, repo.container_id, number, n.approvals(reviews))


async def sync_markdown(workspace_id: str, repo: Repo, gh: GitHub) -> int:
    """Diff the default branch's docs by blob SHA; fetch only blobs that changed."""
    branch = await get_json(gh, f"/repos/{repo.full_name}/branches/{repo.default_branch}")
    commit_sha = branch["commit"]["sha"]
    tree_sha = branch["commit"]["commit"]["tree"]["sha"]
    tree = await get_json(gh, f"/repos/{repo.full_name}/git/trees/{tree_sha}", {"recursive": "1"})
    if tree.get("truncated"):
        log.warning("Tree for %s is truncated; some docs may be missed", repo.full_name)
    wanted = {
        e["path"]: e["sha"]
        for e in tree.get("tree", [])
        if e.get("type") == "blob" and n.matches_doc_globs(e["path"], repo.doc_globs)
    }

    async with worker_session(workspace_id) as s:
        existing = {
            row[0]: row[1]
            for row in await s.execute(
                text(
                    "SELECT metadata->>'path', metadata->>'blob_sha' FROM source_document"
                    " WHERE container_id = :cid AND kind = 'markdown_file' AND deleted_at IS NULL"
                ),
                {"cid": repo.container_id},
            )
        }

    changed = [p for p, sha in wanted.items() if existing.get(p) != sha]
    docs = []
    for path in changed:
        blob = await get_json(gh, f"/repos/{repo.full_name}/git/blobs/{wanted[path]}")
        content = base64.b64decode(blob["content"]).decode("utf-8", errors="replace")
        docs.append(
            n.normalize_markdown_file(
                repo=repo.as_json(),
                path=path,
                content=content,
                blob_sha=wanted[path],
                commit_sha=commit_sha,
            )
        )
    await upsert_documents(workspace_id, repo.container_id, docs)
    removed = [f"{repo.repo_id}:{p}" for p in existing if p not in wanted]
    await tombstone(workspace_id, removed)
    return len(docs)
