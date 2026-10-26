"""Installation, channel and permission sync for the Slack connector.

Everything runs as tacit_worker inside one workspace. Functions take the Slack client as
an argument so tests can point them at a mocked API.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from ...db import worker_session
from . import normalize as n
from .client import SlackClient, bot_client, get_all
from .tokens import decrypt_token, encrypt_token

log = logging.getLogger(__name__)


class InstallVerificationError(Exception):
    pass


@dataclass(frozen=True)
class Installation:
    workspace_id: str
    team_id: str
    team_domain: str | None
    bot_user_id: str
    token: str


@dataclass(frozen=True)
class ChannelRef:
    container_id: str
    team_id: str
    channel_id: str
    name: str
    is_private: bool
    is_archived: bool
    team_domain: str | None

    def as_channel(self) -> n.Channel:
        return n.Channel(self.team_id, self.channel_id, self.name, self.team_domain)


# --- Identity -------------------------------------------------------------------------


async def linked_slack_accounts(workspace_id: str, user_id: str) -> list[str]:
    """The user's OAuth-verified Slack links, as "T:U" (Better Auth account rows)."""
    async with worker_session(workspace_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        'SELECT "accountId" FROM account WHERE "userId" = :u'
                        " AND \"providerId\" = 'slack'"
                    ),
                    {"u": user_id},
                )
            ).scalars()
        )


def verify_installer(oauth: dict[str, Any], linked: list[str]) -> None:
    """The Slack user who approved the install must be the Tacit user who started it."""
    if oauth.get("is_enterprise_install"):
        raise InstallVerificationError("Enterprise Grid org-wide installs aren't supported")
    team_id = (oauth.get("team") or {}).get("id")
    installer = (oauth.get("authed_user") or {}).get("id")
    if not team_id or not installer or f"{team_id}:{installer}" not in linked:
        raise InstallVerificationError("Installer isn't your linked Slack account")
    if not (oauth.get("access_token") and oauth.get("bot_user_id")):
        raise InstallVerificationError("No bot token in the OAuth response")


# --- Installations --------------------------------------------------------------------


async def upsert_installation(
    workspace_id: str, oauth: dict[str, Any], team_info: dict[str, Any], user_id: str
) -> None:
    """Link a Slack team to a workspace. If another workspace already owns the team, RLS
    hides that row and the conflicting upsert errors instead of re-pointing it."""
    team = oauth["team"]
    token = oauth["access_token"]
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO slack_installation (workspace_id, team_id, team_name, team_domain,"
                " enterprise_id, app_id, bot_user_id, scopes, bot_token_encrypted,"
                " installed_by_user_id, installed_by_slack_user_id)"
                " VALUES (:ws, :team, :name, :domain, :ent, :app, :bot, :scopes, :tok, :uid, :su)"
                " ON CONFLICT (team_id) DO UPDATE SET team_name = excluded.team_name,"
                " team_domain = excluded.team_domain, app_id = excluded.app_id,"
                " bot_user_id = excluded.bot_user_id, scopes = excluded.scopes,"
                " bot_token_encrypted = excluded.bot_token_encrypted,"
                " installed_by_user_id = excluded.installed_by_user_id,"
                " installed_by_slack_user_id = excluded.installed_by_slack_user_id,"
                " revoked_at = NULL, updated_at = now()"
            ),
            {
                "ws": workspace_id,
                "team": team["id"],
                "name": team.get("name") or team_info.get("name") or team["id"],
                "domain": team_info.get("domain"),
                "ent": (oauth.get("enterprise") or {}).get("id"),
                "app": oauth.get("app_id"),
                "bot": oauth["bot_user_id"],
                "scopes": [x for x in (oauth.get("scope") or "").split(",") if x],
                "tok": encrypt_token(token, team["id"]),
                "uid": user_id,
                "su": oauth["authed_user"]["id"],
            },
        )


async def load_installation(workspace_id: str, team_id: str) -> Installation | None:
    async with worker_session(workspace_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT team_domain, bot_user_id, bot_token_encrypted FROM slack_installation"
                    " WHERE team_id = :t AND revoked_at IS NULL"
                ),
                {"t": team_id},
            )
        ).one_or_none()
    if row is None or row.bot_token_encrypted is None:
        return None
    token = decrypt_token(row.bot_token_encrypted, team_id)
    return Installation(workspace_id, team_id, row.team_domain, row.bot_user_id, token)


def client_for(inst: Installation) -> SlackClient:
    return bot_client(inst.token)


async def revoke_installation(workspace_id: str, team_id: str) -> None:
    """Uninstall / token revoked: cut access, hide content, and wipe the token."""
    await deactivate_channels(workspace_id, team_id, None)
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE slack_installation SET revoked_at = now(), bot_token_encrypted = NULL,"
                " updated_at = now() WHERE team_id = :t"
            ),
            {"t": team_id},
        )


# --- Channels -------------------------------------------------------------------------


def _channel_url(domain: str | None, channel_id: str) -> str:
    host = f"https://{domain}.slack.com" if domain else "https://slack.com"
    return f"{host}/archives/{channel_id}"


async def upsert_channel(workspace_id: str, inst: Installation, ch: dict[str, Any]) -> ChannelRef:
    is_private = bool(ch.get("is_private") or ch.get("is_group"))
    name = f"#{ch.get('name') or ch['id']}"
    async with worker_session(workspace_id) as s:
        cid = await s.scalar(
            text(
                "INSERT INTO source_container (workspace_id, source, external_id, name, url,"
                " visibility, is_active) VALUES (:ws, 'slack', :ext, :name, :url, :vis, true)"
                " ON CONFLICT (workspace_id, source, external_id) DO UPDATE SET"
                " name = excluded.name, url = excluded.url, visibility = excluded.visibility,"
                " is_active = true, updated_at = now() RETURNING id::text"
            ),
            {
                "ws": workspace_id,
                "ext": f"{inst.team_id}:{ch['id']}",
                "name": name,
                "url": _channel_url(inst.team_domain, ch["id"]),
                # Never 'public': a public Slack channel is public only inside the team.
                "vis": "private" if is_private else "internal",
            },
        )
        await s.execute(
            text(
                "INSERT INTO slack_channel (container_id, workspace_id, team_id, channel_id,"
                " name, is_private, is_archived) VALUES (:cid, :ws, :team, :ch, :name, :priv, :arch)"
                " ON CONFLICT (container_id) DO UPDATE SET name = excluded.name,"
                " is_private = excluded.is_private, is_archived = excluded.is_archived"
            ),
            {
                "cid": cid,
                "ws": workspace_id,
                "team": inst.team_id,
                "ch": ch["id"],
                "name": name,
                "priv": is_private,
                "arch": bool(ch.get("is_archived")),
            },
        )
    return ChannelRef(
        cid,
        inst.team_id,
        ch["id"],
        name,
        is_private,
        bool(ch.get("is_archived")),
        inst.team_domain,
    )


async def deactivate_channels(
    workspace_id: str, team_id: str, channel_ids: list[str] | None
) -> int:
    """Cut access first, then hide content, in one transaction. `None` = whole team."""
    async with worker_session(workspace_id) as s:
        filt = "" if channel_ids is None else " AND external_id = ANY(:ext)"
        params: dict[str, Any] = {"prefix": f"{team_id}:%"}
        if channel_ids is not None:
            params["ext"] = [f"{team_id}:{c}" for c in channel_ids]
        ids = list(
            (
                await s.execute(
                    text(
                        "UPDATE source_container SET is_active = false, acl_synced_at = NULL,"
                        " updated_at = now() WHERE source = 'slack' AND external_id LIKE :prefix"
                        + filt
                        + " RETURNING id"
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


async def sync_channels(workspace_id: str, inst: Installation, client: SlackClient) -> list[str]:
    """Mirror the channels the bot is in. Channels it left are deactivated.
    Returns container ids of channels that still need a backfill."""
    channels = await get_all(
        client.users_conversations,
        "channels",
        types="public_channel,private_channel",
        exclude_archived=False,
    )
    # Shared channels from other orgs (Slack Connect) are out of scope.
    channels = [c for c in channels if not c.get("is_ext_shared")]
    for ch in channels:
        await upsert_channel(workspace_id, inst, ch)
    current = {c["id"] for c in channels}
    async with worker_session(workspace_id) as s:
        known = {
            r[0]: (r[1], r[2])
            for r in await s.execute(
                text(
                    "SELECT sc.channel_id, sc.container_id::text, sc.backfill_status"
                    " FROM slack_channel sc JOIN source_container c ON c.id = sc.container_id"
                    " WHERE sc.team_id = :t AND c.is_active"
                ),
                {"t": inst.team_id},
            )
        }
    if gone := sorted(set(known) - current):
        await deactivate_channels(workspace_id, inst.team_id, gone)
    return [cid for ch_id, (cid, st) in known.items() if ch_id in current and st == "pending"]


async def active_channels(workspace_id: str, team_id: str) -> list[ChannelRef]:
    async with worker_session(workspace_id) as s:
        rows = await s.execute(
            text(
                "SELECT sc.container_id::text, sc.team_id, sc.channel_id, sc.name,"
                " sc.is_private, sc.is_archived, i.team_domain FROM slack_channel sc"
                " JOIN source_container c ON c.id = sc.container_id"
                " JOIN slack_installation i ON i.team_id = sc.team_id"
                " WHERE sc.team_id = :t AND c.is_active AND sc.sync_enabled"
            ),
            {"t": team_id},
        )
        return [ChannelRef(*r) for r in rows]


async def load_channel(workspace_id: str, container_id: str) -> ChannelRef | None:
    async with worker_session(workspace_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT sc.container_id::text, sc.team_id, sc.channel_id, sc.name,"
                    " sc.is_private, sc.is_archived, i.team_domain FROM slack_channel sc"
                    " JOIN source_container c ON c.id = sc.container_id"
                    " JOIN slack_installation i ON i.team_id = sc.team_id"
                    " WHERE sc.container_id = :cid AND c.is_active AND sc.sync_enabled"
                    " AND i.revoked_at IS NULL"
                ),
                {"cid": container_id},
            )
        ).one_or_none()
    return ChannelRef(*row) if row else None


# --- Directory + permissions ----------------------------------------------------------


async def upsert_directory(workspace_id: str, team_id: str, users: list[dict[str, Any]]) -> None:
    rows = [
        {
            "ws": workspace_id,
            "ext": f"{team_id}:{u['id']}",
            "login": n.handle(u),
            "name": (u.get("profile") or {}).get("real_name") or u.get("real_name"),
            "avatar": (u.get("profile") or {}).get("image_72"),
            "bot": bool(u.get("is_bot") or u.get("id") == n.SLACKBOT),
        }
        for u in users
        if u.get("id")
    ]
    if not rows:
        return
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO external_identity (workspace_id, source, external_id, login,"
                " display_name, avatar_url, is_bot) VALUES (:ws, 'slack', :ext, :login, :name,"
                " :avatar, :bot) ON CONFLICT (workspace_id, source, external_id) DO UPDATE SET"
                " login = excluded.login, display_name = excluded.display_name,"
                " avatar_url = excluded.avatar_url, is_bot = excluded.is_bot, updated_at = now()"
            ),
            rows,
        )


async def load_directory(workspace_id: str, team_id: str) -> n.Directory:
    """Mention map from the stored directory (no API call; used by webhook handlers)."""
    async with worker_session(workspace_id) as s:
        rows = await s.execute(
            text(
                "SELECT split_part(external_id, ':', 2), login, is_bot FROM external_identity"
                " WHERE source = 'slack' AND external_id LIKE :p"
            ),
            {"p": f"{team_id}:%"},
        )
        users, bots = {}, set()
        for uid, login, is_bot in rows:
            users[uid] = login or uid
            if is_bot:
                bots.add(uid)
    return n.Directory(users=users, bots=frozenset(bots))


def allowed_slack_users(
    channel: ChannelRef, members: list[str], users_by_id: dict[str, dict[str, Any]]
) -> set[str]:
    """Slack user ids that can read this channel in Slack (pure; see design §5)."""
    active = {uid for uid, u in users_by_id.items() if n.is_active_human(u)}
    allowed = set(members) & active
    if not channel.is_private:
        allowed |= {uid for uid, u in users_by_id.items() if n.is_full_member(u)}
    return allowed


async def sync_team_acl(workspace_id: str, team_id: str, client: SlackClient) -> int:
    """Replace container_access for every active channel of the team with: Slack users
    who can read it ∩ verified linked accounts ∩ workspace members.

    All Slack calls happen first; then one transaction writes everything. An API error
    writes nothing, and the old ACL goes stale after 6 hours (fail closed).
    """
    users = await get_all(client.users_list, "members")
    users_by_id = {u["id"]: u for u in users if u.get("id")}
    channels = await active_channels(workspace_id, team_id)
    members = {
        ch.container_id: await get_all(
            client.conversations_members, "members", channel=ch.channel_id
        )
        for ch in channels
    }
    await upsert_directory(workspace_id, team_id, users)

    total = 0
    async with worker_session(workspace_id) as s:
        linked = {
            account_id.split(":", 1)[1]: user_id
            for user_id, account_id in await s.execute(
                text(
                    'SELECT a."userId", a."accountId" FROM account a'
                    ' JOIN member m ON m."userId" = a."userId"'
                    ' WHERE a."providerId" = \'slack\' AND a."accountId" LIKE :p'
                    ' AND m."organizationId" = :ws'
                ),
                {"p": f"{team_id}:%", "ws": workspace_id},
            )
        }
        for ch in channels:
            allowed = allowed_slack_users(ch, members[ch.container_id], users_by_id)
            rows = [
                {
                    "cid": ch.container_id,
                    "uid": linked[slack_id],
                    "ws": workspace_id,
                    "perm": "member" if slack_id in members[ch.container_id] else "read",
                    "via": f"{team_id}:{slack_id}",
                }
                for slack_id in sorted(allowed & linked.keys())
            ]
            await s.execute(
                text("DELETE FROM container_access WHERE container_id = :cid"),
                {"cid": ch.container_id},
            )
            if rows:
                await s.execute(
                    text(
                        "INSERT INTO container_access (container_id, user_id, workspace_id,"
                        " permission, via_external_id) VALUES (:cid, :uid, :ws, :perm, :via)"
                        " ON CONFLICT (container_id, user_id) DO NOTHING"
                    ),
                    rows,
                )
            await s.execute(
                text("UPDATE source_container SET acl_synced_at = now() WHERE id = :cid"),
                {"cid": ch.container_id},
            )
            total += len(rows)
    return total


async def revoke_member_access(
    workspace_id: str, team_id: str, channel_id: str, slack_user_id: str
) -> None:
    """member_left_channel: drop that person's access now, before any resync."""
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "DELETE FROM container_access a USING source_container c"
                " WHERE a.container_id = c.id AND c.source = 'slack' AND c.external_id = :ext"
                " AND a.via_external_id = :via"
            ),
            {"ext": f"{team_id}:{channel_id}", "via": f"{team_id}:{slack_user_id}"},
        )
