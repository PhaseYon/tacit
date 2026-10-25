"""Installation, repository and permission sync for the GitHub connector.

Everything here runs as tacit_worker inside a single workspace. Functions take the
GitHub client as an argument so tests can point them at a mocked API.
"""

import logging
from dataclasses import dataclass
from typing import Any

from githubkit import GitHub
from githubkit.exception import RequestFailed
from sqlalchemy import text

from ...db import worker_session
from .api import get_all, get_json

log = logging.getLogger(__name__)


class InstallVerificationError(Exception):
    pass


@dataclass(frozen=True)
class RepoRef:
    container_id: str
    full_name: str
    visibility: str


async def linked_github_id(workspace_id: str, user_id: str) -> int | None:
    """The user's GitHub id, proven by OAuth (Better Auth's account table)."""
    async with worker_session(workspace_id) as s:
        value = await s.scalar(
            text(
                'SELECT "accountId" FROM account WHERE "userId" = :u AND "providerId" = \'github\''
            ),
            {"u": user_id},
        )
    return int(value) if value else None


async def verify_installer(
    app_gh: GitHub, install_gh: GitHub, installation_id: int, github_user_id: int
) -> dict[str, Any]:
    """Check the Tacit user may link this installation. The installation_id in the
    setup redirect is unauthenticated, so we never trust it on its own.

    Personal account: the user must BE that account. Organization: the user must be
    an org admin. Returns the installation JSON.
    """
    inst = await get_json(app_gh, f"/app/installations/{installation_id}")
    account = inst["account"]
    if inst.get("target_type", account.get("type")) == "User":
        if account["id"] != github_user_id:
            raise InstallVerificationError("Installed on a different GitHub account")
        return inst
    user = await get_json(install_gh, f"/user/{github_user_id}")
    try:
        membership = await get_json(
            install_gh, f"/orgs/{account['login']}/memberships/{user['login']}"
        )
    except RequestFailed as exc:
        raise InstallVerificationError("Not a member of the organization") from exc
    if membership.get("state") != "active" or membership.get("role") != "admin":
        raise InstallVerificationError("Only organization admins can connect it")
    return inst


async def upsert_installation(workspace_id: str, inst: dict[str, Any], user_id: str) -> None:
    """Link an installation to a workspace. Fails if another workspace already owns it
    (RLS hides that row, so the conflicting insert errors instead of re-pointing it)."""
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO github_installation (workspace_id, installation_id, account_id,"
                " account_login, account_type, repository_selection, permissions, events,"
                " installed_by_user_id, suspended_at, deleted_at)"
                " VALUES (:ws, :iid, :aid, :login, :atype, :sel, CAST(:perms AS jsonb),"
                " CAST(:events AS jsonb), :uid, NULL, NULL)"
                " ON CONFLICT (installation_id) DO UPDATE SET"
                " account_login = excluded.account_login,"
                " repository_selection = excluded.repository_selection,"
                " permissions = excluded.permissions, events = excluded.events,"
                " deleted_at = NULL, suspended_at = NULL, updated_at = now()"
            ),
            _installation_params(workspace_id, inst, user_id),
        )


def _installation_params(workspace_id: str, inst: dict[str, Any], user_id: str | None):
    import json

    return {
        "ws": workspace_id,
        "iid": inst["id"],
        "aid": inst["account"]["id"],
        "login": inst["account"]["login"],
        "atype": inst["account"].get("type", "User"),
        "sel": inst.get("repository_selection"),
        "perms": json.dumps(inst.get("permissions") or {}),
        "events": json.dumps(inst.get("events") or []),
        "uid": user_id,
    }


def _visibility(repo: dict[str, Any]) -> str:
    v = repo.get("visibility") or ("private" if repo.get("private") else "public")
    return v if v in ("public", "private", "internal") else "private"


async def upsert_repo(workspace_id: str, installation_id: int, repo: dict[str, Any]) -> RepoRef:
    async with worker_session(workspace_id) as s:
        cid = await s.scalar(
            text(
                "INSERT INTO source_container (workspace_id, source, external_id, name, url,"
                " visibility, is_active) VALUES (:ws, 'github', :ext, :name, :url, :vis, true)"
                " ON CONFLICT (workspace_id, source, external_id) DO UPDATE SET"
                " name = excluded.name, url = excluded.url, visibility = excluded.visibility,"
                " is_active = true, updated_at = now()"
                " RETURNING id::text"
            ),
            {
                "ws": workspace_id,
                "ext": str(repo["id"]),
                "name": repo["full_name"],
                "url": repo["html_url"],
                "vis": _visibility(repo),
            },
        )
        await s.execute(
            text(
                "INSERT INTO github_repository (container_id, workspace_id, installation_id,"
                " repo_id, node_id, full_name, default_branch, archived)"
                " VALUES (:cid, :ws, :iid, :rid, :node, :name, :branch, :archived)"
                " ON CONFLICT (container_id) DO UPDATE SET installation_id = excluded.installation_id,"
                " full_name = excluded.full_name, default_branch = excluded.default_branch,"
                " archived = excluded.archived"
            ),
            {
                "cid": cid,
                "ws": workspace_id,
                "iid": installation_id,
                "rid": repo["id"],
                "node": repo["node_id"],
                "name": repo["full_name"],
                "branch": repo.get("default_branch") or "main",
                "archived": bool(repo.get("archived")),
            },
        )
    return RepoRef(cid, repo["full_name"], _visibility(repo))


async def deactivate_containers(workspace_id: str, external_ids: list[str] | None) -> int:
    """Cut access first, then hide content. `None` means every GitHub container in the
    workspace (uninstall). Rows are purged later by a cleanup job."""
    async with worker_session(workspace_id) as s:
        filt = "" if external_ids is None else " AND external_id = ANY(:ids)"
        params: dict[str, Any] = {"ids": external_ids} if external_ids is not None else {}
        ids = list(
            (
                await s.execute(
                    text(
                        "UPDATE source_container SET is_active = false, acl_synced_at = NULL,"
                        " updated_at = now() WHERE source = 'github'" + filt + " RETURNING id"
                    ),
                    params,
                )
            ).scalars()
        )
        if ids:
            await s.execute(
                text("DELETE FROM container_access WHERE container_id = ANY(:ids)"), {"ids": ids}
            )
            await s.execute(
                text(
                    "UPDATE source_document SET deleted_at = now()"
                    " WHERE container_id = ANY(:ids) AND deleted_at IS NULL"
                ),
                {"ids": ids},
            )
    return len(ids)


async def sync_installation_repos(
    workspace_id: str, installation_id: int, gh: GitHub
) -> list[RepoRef]:
    """Mirror the installation's repo list. Repos no longer granted are deactivated."""
    repos = await get_all(gh, "/installation/repositories", {"per_page": 100}, key="repositories")
    refs = [await upsert_repo(workspace_id, installation_id, r) for r in repos]
    current = {str(r["id"]) for r in repos}
    async with worker_session(workspace_id) as s:
        known = set(
            (
                await s.execute(
                    text(
                        "SELECT c.external_id FROM source_container c"
                        " JOIN github_repository g ON g.container_id = c.id"
                        " WHERE g.installation_id = :iid AND c.is_active"
                    ),
                    {"iid": installation_id},
                )
            ).scalars()
        )
    if gone := sorted(known - current):
        await deactivate_containers(workspace_id, gone)
    return refs


async def sync_repo_acl(workspace_id: str, repo: RepoRef, gh: GitHub) -> int:
    """Replace the repo's access rows with: GitHub collaborators (any affiliation) who
    have a verified linked account AND are members of this workspace.

    Runs in one transaction, so a failure never leaves partial access behind.
    """
    collaborators = await get_all(
        gh, f"/repos/{repo.full_name}/collaborators", {"affiliation": "all", "per_page": 100}
    )
    by_github_id = {
        str(c["id"]): c.get("role_name") or "read" for c in collaborators if c.get("id")
    }
    async with worker_session(workspace_id) as s:
        rows = (
            await s.execute(
                text(
                    'SELECT a."userId", a."accountId" FROM account a'
                    ' JOIN member m ON m."userId" = a."userId"'
                    ' WHERE a."providerId" = \'github\' AND a."accountId" = ANY(:ids)'
                    ' AND m."organizationId" = :ws'
                ),
                {"ids": list(by_github_id), "ws": workspace_id},
            )
        ).all()
        await s.execute(
            text("DELETE FROM container_access WHERE container_id = :cid"),
            {"cid": repo.container_id},
        )
        for user_id, github_id in rows:
            await s.execute(
                text(
                    "INSERT INTO container_access (container_id, user_id, workspace_id,"
                    " permission, via_external_id) VALUES (:cid, :uid, :ws, :perm, :gid)"
                ),
                {
                    "cid": repo.container_id,
                    "uid": user_id,
                    "ws": workspace_id,
                    "perm": by_github_id[github_id],
                    "gid": github_id,
                },
            )
        await s.execute(
            text("UPDATE source_container SET acl_synced_at = now() WHERE id = :cid"),
            {"cid": repo.container_id},
        )
    return len(rows)


async def active_repos(workspace_id: str, installation_id: int | None = None) -> list[RepoRef]:
    async with worker_session(workspace_id) as s:
        filt = " AND g.installation_id = :iid" if installation_id else ""
        rows = await s.execute(
            text(
                "SELECT c.id::text, g.full_name, c.visibility FROM source_container c"
                " JOIN github_repository g ON g.container_id = c.id"
                " WHERE c.is_active AND g.sync_enabled" + filt
            ),
            {"iid": installation_id},
        )
        return [RepoRef(*r) for r in rows]
