"""Connections, page trees and access (ACL) for the Notion connector.

Everything runs as tacit_worker inside one workspace. The permission model (design §1):
a connection's trees are readable by its owner only, plus every workspace member for
trees an admin-owner marked workspace_visible. Access rows come from Tacit state alone,
so writing them needs no Notion call.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import worker_session
from . import normalize as n
from .client import NotionClient, NotionError, connect, exchange, is_unauthorized
from .tokens import decrypt_token, encrypt_token

log = logging.getLogger(__name__)


class ConnectError(Exception):
    pass


@dataclass(frozen=True)
class Connection:
    workspace_id: str
    id: str
    user_id: str
    notion_workspace_id: str
    notion_user_id: str
    bot_id: str
    access_token: str

    def context(self, users: dict[str, str]) -> n.Context:
        return n.Context(self.id, self.notion_workspace_id, users)


# --- Connections ----------------------------------------------------------------------


def verify_oauth(oauth: dict[str, Any]) -> None:
    """A connection must belong to a person: that's who its content is visible to."""
    owner = oauth.get("owner") or {}
    if owner.get("type") != "user" or not (owner.get("user") or {}).get("id"):
        raise ConnectError("Only user-owned Notion connections are supported")
    if not (oauth.get("access_token") and oauth.get("bot_id") and oauth.get("workspace_id")):
        raise ConnectError("Incomplete OAuth response from Notion")


async def upsert_connection(workspace_id: str, user_id: str, oauth: dict[str, Any]) -> str:
    """Store (or refresh, on reconnect) the user's connection. A bot_id already linked to
    another Tacit workspace makes the insert fail (UNIQUE), so it can't be re-pointed."""
    verify_oauth(oauth)
    bot = oauth["bot_id"]
    person = oauth["owner"]["user"]
    refresh = oauth.get("refresh_token")
    async with worker_session(workspace_id) as s:
        cid = await s.scalar(
            text(
                "INSERT INTO notion_connection (workspace_id, user_id, notion_workspace_id,"
                " notion_workspace_name, notion_user_id, notion_user_name, bot_id,"
                " access_token_encrypted, refresh_token_encrypted) VALUES (:ws, :u, :nws,"
                " :nws_name, :nu, :nu_name, :bot, :tok, :ref)"
                " ON CONFLICT (workspace_id, user_id, notion_workspace_id) DO UPDATE SET"
                " notion_workspace_name = excluded.notion_workspace_name,"
                " notion_user_id = excluded.notion_user_id,"
                " notion_user_name = excluded.notion_user_name, bot_id = excluded.bot_id,"
                " access_token_encrypted = excluded.access_token_encrypted,"
                " refresh_token_encrypted = excluded.refresh_token_encrypted,"
                " revoked_at = NULL, updated_at = now() RETURNING id::text"
            ),
            {
                "ws": workspace_id,
                "u": user_id,
                "nws": oauth["workspace_id"],
                "nws_name": oauth.get("workspace_name"),
                "nu": person["id"],
                "nu_name": person.get("name"),
                "bot": bot,
                "tok": encrypt_token(oauth["access_token"], bot, "access"),
                "ref": encrypt_token(refresh, bot, "refresh") if refresh else None,
            },
        )
        await _upsert_identities(s, workspace_id, oauth["workspace_id"], [person])
    return cid


async def load_connection(workspace_id: str, connection_id: str) -> Connection | None:
    async with worker_session(workspace_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT user_id, notion_workspace_id, notion_user_id, bot_id,"
                    " access_token_encrypted FROM notion_connection"
                    " WHERE id = :c AND revoked_at IS NULL"
                ),
                {"c": connection_id},
            )
        ).one_or_none()
    if row is None or row.access_token_encrypted is None:
        return None
    token = decrypt_token(row.access_token_encrypted, row.bot_id, "access")
    return Connection(
        workspace_id,
        connection_id,
        row.user_id,
        row.notion_workspace_id,
        row.notion_user_id,
        row.bot_id,
        token,
    )


async def refresh_access(workspace_id: str, connection_id: str, used_token: str) -> bool:
    """After a 401: rotate the token pair once, under a row lock. If another worker
    already rotated it, use theirs. If Notion refuses, revoke (fail closed)."""
    async with worker_session(workspace_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT bot_id, access_token_encrypted, refresh_token_encrypted"
                    " FROM notion_connection WHERE id = :c AND revoked_at IS NULL FOR UPDATE"
                ),
                {"c": connection_id},
            )
        ).one_or_none()
        if row is None:
            return False
        if decrypt_token(row.access_token_encrypted, row.bot_id, "access") != used_token:
            return True
        resp: dict[str, Any] | None = None
        if row.refresh_token_encrypted is not None:
            try:
                resp = await exchange(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": decrypt_token(
                            row.refresh_token_encrypted, row.bot_id, "refresh"
                        ),
                    }
                )
            except NotionError:  # refused; network errors propagate and retry later
                resp = None
        if resp and resp.get("access_token"):
            await s.execute(
                text(
                    "UPDATE notion_connection SET access_token_encrypted = :tok,"
                    " refresh_token_encrypted = COALESCE(:ref, refresh_token_encrypted),"
                    " updated_at = now() WHERE id = :c"
                ),
                {
                    "c": connection_id,
                    "tok": encrypt_token(resp["access_token"], row.bot_id, "access"),
                    "ref": encrypt_token(resp["refresh_token"], row.bot_id, "refresh")
                    if resp.get("refresh_token")
                    else None,
                },
            )
            return True
    log.warning("Notion connection %s lost authorization; revoking", connection_id)
    await revoke_connection(workspace_id, connection_id)
    return False


async def with_connection[R](
    workspace_id: str,
    connection_id: str,
    fn: Callable[[Connection, NotionClient], Awaitable[R]],
) -> R | None:
    """Run `fn` with a live client. On 401: refresh once and retry; a second 401 (or a
    failed refresh) revokes the connection. Returns None if there's no live connection."""
    for attempt in range(2):
        conn = await load_connection(workspace_id, connection_id)
        if conn is None:
            return None
        try:
            async with connect(conn.access_token) as client:
                return await fn(conn, client)
        except Exception as exc:
            if not is_unauthorized(exc):
                raise
            if attempt or not await refresh_access(workspace_id, connection_id, conn.access_token):
                await revoke_connection(workspace_id, connection_id)
                return None
    return None


async def revoke_connection(workspace_id: str, connection_id: str) -> None:
    """Disconnect / token revoked: cut access, hide content, wipe tokens, in one
    transaction. Bookkeeping is dropped so a reconnect re-fetches everything."""
    async with worker_session(workspace_id) as s:
        ids = list(
            (
                await s.execute(
                    text(
                        "UPDATE source_container c SET is_active = false, acl_synced_at = NULL,"
                        " updated_at = now() FROM notion_tree t WHERE t.container_id = c.id"
                        " AND t.connection_id = :c RETURNING c.id"
                    ),
                    {"c": connection_id},
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
        await s.execute(
            text(
                "UPDATE notion_connection SET revoked_at = now(), access_token_encrypted = NULL,"
                " refresh_token_encrypted = NULL, updated_at = now() WHERE id = :c"
            ),
            {"c": connection_id},
        )
        await s.execute(
            text("DELETE FROM notion_page WHERE connection_id = :c"), {"c": connection_id}
        )
        await s.execute(
            text("DELETE FROM notion_sync_cursor WHERE connection_id = :c"), {"c": connection_id}
        )


# --- Directory ------------------------------------------------------------------------


async def _upsert_identities(
    s: AsyncSession, workspace_id: str, notion_ws: str, users: list[dict[str, Any]]
) -> None:
    rows = [
        {
            "ws": workspace_id,
            "ext": f"{notion_ws}:{u['id']}",
            "name": u.get("name"),
            "avatar": u.get("avatar_url"),
            "bot": u.get("type") == "bot",
        }
        for u in users
        if u.get("id")
    ]
    if rows:
        await s.execute(
            text(
                "INSERT INTO external_identity (workspace_id, source, external_id, login,"
                " display_name, avatar_url, is_bot) VALUES (:ws, 'notion', :ext, :name, :name,"
                " :avatar, :bot) ON CONFLICT (workspace_id, source, external_id) DO UPDATE SET"
                " login = excluded.login, display_name = excluded.display_name,"
                " avatar_url = excluded.avatar_url, is_bot = excluded.is_bot, updated_at = now()"
            ),
            rows,
        )


async def upsert_directory(workspace_id: str, notion_ws: str, users: list[dict[str, Any]]) -> None:
    async with worker_session(workspace_id) as s:
        await _upsert_identities(s, workspace_id, notion_ws, users)


async def load_users(workspace_id: str, notion_ws: str) -> dict[str, str]:
    async with worker_session(workspace_id) as s:
        rows = await s.execute(
            text(
                "SELECT split_part(external_id, ':', 2), login FROM external_identity"
                " WHERE source = 'notion' AND external_id LIKE :p"
            ),
            {"p": f"{notion_ws}:%"},
        )
        return {r[0]: r[1] for r in rows if r[1]}


# --- Trees + access -------------------------------------------------------------------


async def recompute_trees(workspace_id: str, connection_id: str) -> dict[str, str]:
    """Assign every known page to its tree's container, create new trees, move pages
    (documents AND their chunks, which carry the container for RLS) and deactivate
    trees left empty. Then rewrite access, all in one transaction.
    Returns page id -> container id."""
    async with worker_session(workspace_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT page_id, object, parent_type, parent_id, database_id, title, url,"
                    " container_id::text FROM notion_page WHERE connection_id = :c"
                ),
                {"c": connection_id},
            )
        ).all()
        info = {r.page_id: r for r in rows}
        roots = n.compute_roots(
            n.Node(r.page_id, r.object, r.parent_type or "workspace", r.parent_id, r.database_id)
            for r in rows
        )
        containers: dict[str, str] = {}
        for root in sorted(set(roots.values())):
            r = info[root]
            cid = await s.scalar(
                text(
                    "INSERT INTO source_container (workspace_id, source, external_id, name, url,"
                    " visibility, is_active) VALUES (:ws, 'notion', :ext, :name, :url,"
                    " 'private', true) ON CONFLICT (workspace_id, source, external_id) DO UPDATE"
                    " SET name = excluded.name, url = excluded.url, visibility = 'private',"
                    " is_active = true, updated_at = now() RETURNING id::text"
                ),
                {
                    "ws": workspace_id,
                    "ext": f"{connection_id}:{root}",
                    "name": r.title or "Untitled",
                    "url": r.url,
                },
            )
            await s.execute(
                text(
                    "INSERT INTO notion_tree (container_id, workspace_id, connection_id, root_id,"
                    " root_type, title) VALUES (:cid, :ws, :c, :root, :type, :title)"
                    " ON CONFLICT (container_id) DO UPDATE SET title = excluded.title,"
                    " root_type = excluded.root_type"
                ),
                {
                    "cid": cid,
                    "ws": workspace_id,
                    "c": connection_id,
                    "root": root,
                    "type": r.object,
                    "title": r.title or "Untitled",
                },
            )
            containers[root] = cid

        moves = [
            {"c": connection_id, "p": pid, "cid": containers[root], "ext": f"{connection_id}:{pid}"}
            for pid, root in roots.items()
            if info[pid].container_id != containers[root]
        ]
        if moves:
            await s.execute(
                text(
                    "UPDATE notion_page SET container_id = CAST(:cid AS uuid)"
                    " WHERE connection_id = :c AND page_id = :p"
                ),
                moves,
            )
            await s.execute(
                text(
                    "UPDATE source_document SET container_id = CAST(:cid AS uuid),"
                    " updated_at = now() WHERE source = 'notion' AND external_id = :ext"
                    " AND container_id <> CAST(:cid AS uuid)"
                ),
                moves,
            )
            await s.execute(
                text(
                    "UPDATE document_chunk k SET container_id = CAST(:cid AS uuid)"
                    " FROM source_document d WHERE k.document_id = d.id AND d.source = 'notion'"
                    " AND d.external_id = :ext AND k.container_id <> CAST(:cid AS uuid)"
                ),
                moves,
            )

        # Trees with no pages any more: cut access and deactivate.
        live = list(containers.values())
        gone = list(
            (
                await s.execute(
                    text(
                        "UPDATE source_container c SET is_active = false, acl_synced_at = NULL,"
                        " updated_at = now() FROM notion_tree t WHERE t.container_id = c.id"
                        " AND t.connection_id = :c AND c.is_active"
                        " AND NOT (c.id::text = ANY(:live)) RETURNING c.id"
                    ),
                    {"c": connection_id, "live": live},
                )
            ).scalars()
        )
        if gone:
            await s.execute(
                text("DELETE FROM container_access WHERE container_id = ANY(:ids)"), {"ids": gone}
            )
        await write_acl(s, workspace_id, connection_id)
    return {pid: containers[root] for pid, root in roots.items()}


async def write_acl(s: AsyncSession, workspace_id: str, connection_id: str) -> int:
    """Replace container_access for the connection's active trees: the owner (if still
    a workspace member and the connection is live), plus every workspace member for
    trees marked workspace_visible. Caller owns the transaction."""
    trees = list(
        (
            await s.execute(
                text(
                    "SELECT t.container_id FROM notion_tree t JOIN source_container c"
                    " ON c.id = t.container_id WHERE t.connection_id = :c AND c.is_active"
                ),
                {"c": connection_id},
            )
        ).scalars()
    )
    if not trees:
        return 0
    await s.execute(
        text("DELETE FROM container_access WHERE container_id = ANY(:ids)"), {"ids": trees}
    )
    inserted = await s.execute(
        text(
            "INSERT INTO container_access (container_id, user_id, workspace_id, permission,"
            " via_external_id)"
            ' SELECT t.container_id, m."userId", :ws,'
            "  CASE WHEN m.\"userId\" = nc.user_id THEN 'owner' ELSE 'workspace' END,"
            '  CASE WHEN m."userId" = nc.user_id'
            "   THEN nc.notion_workspace_id || ':' || nc.notion_user_id END"
            " FROM notion_tree t JOIN notion_connection nc ON nc.id = t.connection_id"
            ' JOIN member m ON m."organizationId" = :ws'
            " WHERE t.container_id = ANY(:ids) AND nc.revoked_at IS NULL"
            '  AND (m."userId" = nc.user_id OR t.workspace_visible)'
            " ON CONFLICT (container_id, user_id) DO NOTHING"
        ),
        {"ws": workspace_id, "ids": trees},
    )
    await s.execute(
        text("UPDATE source_container SET acl_synced_at = now() WHERE id = ANY(:ids)"),
        {"ids": trees},
    )
    return inserted.rowcount or 0


async def sync_acl(workspace_id: str, connection_id: str) -> int:
    async with worker_session(workspace_id) as s:
        return await write_acl(s, workspace_id, connection_id)


async def set_tree_visibility(
    workspace_id: str, container_id: str, visible: bool, actor_user_id: str
) -> None:
    """Flip the flag and rewrite access in the same transaction. The caller checked who
    may do this (routes.py: widening needs admin AND connection owner)."""
    async with worker_session(workspace_id) as s:
        connection_id = await s.scalar(
            text(
                "UPDATE notion_tree SET workspace_visible = :v,"
                " marked_by_user_id = CASE WHEN :v THEN :actor END,"
                " marked_at = CASE WHEN :v THEN now() END"
                " WHERE container_id = :cid RETURNING connection_id::text"
            ),
            {"v": visible, "actor": actor_user_id, "cid": container_id},
        )
        if connection_id is not None:
            await write_acl(s, workspace_id, connection_id)
