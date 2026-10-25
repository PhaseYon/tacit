"""Load the Larkspur Labs demo dataset (demo/data/github/*.json) into a workspace.

    uv run python -m tacit_api.demo --workspace <slug-or-id> [--grant-all-members]

The JSON uses GitHub REST API shapes, so it goes through the same normalizers and upserts
as a real backfill. No network calls, no LLM calls, no randomness: loading twice leaves
the same rows (only the ACL freshness timestamp moves).

The demo installation is created *suspended*, so scheduled reconcile/ACL jobs (which call
the real GitHub API) skip it. Private-repo access comes from `container_access` rows we
write here instead of a GitHub collaborator sync. Access data counts as fresh for 6 hours
(see app.can_read_container), so re-run the loader to refresh it.
"""

import argparse
import asyncio
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import text

from .config import get_settings
from .connectors.documents import NormalizedDocument
from .connectors.github import ingest, sync
from .connectors.github import normalize as n
from .db import dispose_engine, worker_session
from .search.indexer import index_workspace

DEMO_ACCOUNT = {"id": 91_000_000, "login": "larkspur", "type": "Organization"}
REPOS = ("handbook", "api", "web")


def demo_dir() -> Path:
    if env := os.environ.get("TACIT_DEMO_DIR"):
        return Path(env)
    return Path(__file__).resolve().parents[4] / "demo"


def load_repo_files(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    base = (directory or demo_dir()) / "data" / "github"
    return {name: json.loads((base / f"{name}.json").read_text()) for name in REPOS}


def demo_installation_id(workspace_id: str) -> int:
    """installation_id is globally unique (one install -> one workspace), so each
    workspace gets its own fake id, derived deterministically from the workspace id."""
    digest = int.from_bytes(hashlib.sha256(workspace_id.encode()).digest()[:4], "big")
    return 8_000_000_000 + digest


def _pr_number(review: dict) -> int:
    return int(review["pull_request_url"].rstrip("/").rsplit("/", 1)[-1])


def normalize_repo(data: dict[str, Any]) -> dict[str, list[NormalizedDocument]]:
    """Every document the repo yields, in upsert order (parents before comments)."""
    repo = data["repository"]
    issues = [n.normalize_issue(i, repo) for i in data["issues"]]
    prs = [n.normalize_issue(p, repo) for p in data["pull_requests"]]
    comments = [n.normalize_issue_comment(c, repo) for c in data["issue_comments"]]
    review_comments = [n.normalize_pr_review_comment(c, repo) for c in data["review_comments"]]
    reviews = [n.normalize_pr_review(r, _pr_number(r), repo) for r in data["pull_request_reviews"]]
    docs = [
        n.normalize_markdown_file(
            repo=repo, path=f["path"], content=f["content"], blob_sha=f["sha"], commit_sha=None
        )
        for f in data["markdown_files"]
        if n.matches_doc_globs(f["path"], None)
    ]
    groups = {
        "parents": issues + prs,
        "issue_comments": comments,
        "review_comments": review_comments,
        "reviews": reviews,
        "markdown": docs,
    }
    return {k: [d for d in v if d is not None] for k, v in groups.items()}


def approvals_by_pr(data: dict[str, Any]) -> dict[int, list[str]]:
    by_pr: dict[int, list[dict]] = defaultdict(list)
    for r in data["pull_request_reviews"]:
        by_pr[_pr_number(r)].append(r)
    return {number: n.approvals(reviews) for number, reviews in sorted(by_pr.items())}


async def _ensure_installation(workspace_id: str) -> int:
    iid = demo_installation_id(workspace_id)
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO github_installation (workspace_id, installation_id, account_id,"
                " account_login, account_type, repository_selection, suspended_at)"
                " VALUES (:ws, :iid, :aid, :login, :type, 'selected', now())"
                " ON CONFLICT (installation_id) DO NOTHING"
            ),
            {
                "ws": workspace_id,
                "iid": iid,
                "aid": DEMO_ACCOUNT["id"],
                "login": DEMO_ACCOUNT["login"],
                "type": DEMO_ACCOUNT["type"],
            },
        )
    return iid


async def _grant_access(workspace_id: str, container_id: str, user_ids: list[str]) -> None:
    async with worker_session(workspace_id) as s:
        if user_ids:
            await s.execute(
                text(
                    "INSERT INTO container_access (container_id, user_id, workspace_id,"
                    " permission, via_external_id) VALUES (:cid, :uid, :ws, 'read', 'demo')"
                    " ON CONFLICT (container_id, user_id) DO NOTHING"
                ),
                [{"cid": container_id, "uid": u, "ws": workspace_id} for u in user_ids],
            )
        await s.execute(
            text("UPDATE source_container SET acl_synced_at = now() WHERE id = :cid"),
            {"cid": container_id},
        )


async def _mark_backfilled(workspace_id: str, container_id: str) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE github_repository SET backfill_status = 'done'"
                " WHERE container_id = :cid AND backfill_status <> 'done'"
            ),
            {"cid": container_id},
        )


async def load_demo(workspace_id: str, *, private_access_user_ids: list[str]) -> dict:
    """Load all demo repos into `workspace_id` and index them. Returns per-repo counts."""
    iid = await _ensure_installation(workspace_id)
    summary: dict[str, Any] = {"installation_id": iid, "repos": {}}
    for data in load_repo_files().values():
        ref = await sync.upsert_repo(workspace_id, iid, data["repository"])
        groups = normalize_repo(data)
        counts = {}
        for group, docs in groups.items():
            counts[group] = await ingest.upsert_documents(workspace_id, ref.container_id, docs)
        await ingest.link_orphans(workspace_id, ref.container_id)
        for number, approved_by in approvals_by_pr(data).items():
            await ingest.set_pr_approvals(workspace_id, ref.container_id, number, approved_by)
        if ref.visibility != "public":
            await _grant_access(workspace_id, ref.container_id, private_access_user_ids)
        await _mark_backfilled(workspace_id, ref.container_id)
        summary["repos"][ref.full_name] = {
            "container_id": ref.container_id,
            "visibility": ref.visibility,
            **counts,
        }
    result = await index_workspace(workspace_id)
    summary["indexed"] = result.__dict__
    return summary


# --- CLI ---------------------------------------------------------------------


def resolve_workspace(slug_or_id: str) -> str:
    """Workspace id for an id or slug. Organizations are only readable by their members
    (RLS), so this one lookup uses the owner role (MIGRATOR_DATABASE_URL)."""
    import psycopg

    url = get_settings().migrator_database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT id FROM organization WHERE id = %s OR slug = %s ORDER BY id = %s DESC LIMIT 1",
            (slug_or_id, slug_or_id, slug_or_id),
        ).fetchone()
    if row is None:
        raise SystemExit(f"No workspace with id or slug '{slug_or_id}'")
    return row[0]


async def _all_members(workspace_id: str) -> list[str]:
    async with worker_session(workspace_id) as s:
        rows = await s.execute(
            text('SELECT "userId" FROM member WHERE "organizationId" = :w ORDER BY "userId"'),
            {"w": workspace_id},
        )
        return list(rows.scalars())


async def _main(args: argparse.Namespace, workspace_id: str) -> None:
    try:
        users = await _all_members(workspace_id) if args.grant_all_members else []
        summary = await load_demo(workspace_id, private_access_user_ids=users)
    finally:
        await dispose_engine()
    summary["workspace_id"] = workspace_id
    summary["private_access_granted_to"] = users
    print(json.dumps(summary, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description="Load the Larkspur Labs demo dataset.")
    p.add_argument("--workspace", required=True, help="workspace id or slug")
    p.add_argument(
        "--grant-all-members",
        action="store_true",
        help="give every current workspace member access to the private demo repos",
    )
    args = p.parse_args()
    asyncio.run(_main(args, resolve_workspace(args.workspace)))


if __name__ == "__main__":
    main()
