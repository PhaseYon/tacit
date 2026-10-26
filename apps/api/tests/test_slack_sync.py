"""Install verification, channel sync and permission (ACL) sync against a fake Slack API,
ending with the leak test that matters: hybrid search never shows a private channel's
messages to a workspace member who isn't in that channel."""

from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tacit_api.connectors.slack import handlers, ingest, sync
from tacit_api.connectors.slack import normalize as n
from tacit_api.db import dispose_engine, rls_session, worker_session
from tacit_api.search.hybrid import hybrid_search
from tacit_api.search.indexer import index_workspace

from .conftest import MIGRATOR_URL, Seed
from .slack_mock import user

TEAM = "TSYNC"
DAVE, ERIN = "u_dave_slack", "u_erin_slack"
# Slack users: alice, bob (full members), dave (guest), carol (not in Acme on Tacit).
USERS = [
    user("UA", "alice"),
    user("UB", "bob"),
    user("UC", "carol"),
    user("UD", "dave", is_restricted=True),
    user("UX", "ex-employee", deleted=True),
    user("UBOT", "tacit", is_bot=True),
]


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


@pytest.fixture(scope="module")
def people(seed: Seed) -> Seed:
    """Dave (guest in Slack) and Erin (no Slack link) are extra Acme members."""
    now = datetime.now(UTC)
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        for uid, name in [(DAVE, "Dave"), (ERIN, "Erin")]:
            conn.execute(
                'INSERT INTO "user" (id, name, email, "emailVerified") VALUES (%s, %s, %s, true)',
                (uid, name, f"{uid}@example.com"),
            )
            conn.execute(
                'INSERT INTO member (id, "organizationId", "userId", role, "createdAt")'
                " VALUES (%s, %s, %s, 'member', %s)",
                (f"m_{uid}", seed.acme, uid, now),
            )
        for uid, slack in [(seed.alice, "UA"), (seed.bob, "UB"), (seed.carol, "UC"), (DAVE, "UD")]:
            conn.execute(
                'INSERT INTO account (id, "accountId", "providerId", "userId", "accessToken",'
                " \"createdAt\", \"updatedAt\") VALUES (%s, %s, 'slack', %s, 'secret', %s, %s)",
                (f"acc_slack_{uid}", f"{TEAM}:{slack}", uid, now, now),
            )
    return seed


def oauth_response(team: str = TEAM, installer: str = "UA", **extra) -> dict:
    return {
        "ok": True,
        "access_token": "xoxb-sync-token",
        "token_type": "bot",
        "scope": "channels:history,channels:read",
        "bot_user_id": "UBOT",
        "app_id": "A1",
        "team": {"id": team, "name": "Acme Slack"},
        "authed_user": {"id": installer},
        "is_enterprise_install": False,
        **extra,
    }


async def install(seed: Seed, team: str = TEAM) -> sync.Installation:
    await sync.upsert_installation(seed.acme, oauth_response(team), {"domain": "acme"}, seed.alice)
    inst = await sync.load_installation(seed.acme, team)
    assert inst is not None
    return inst


# --- install verification -------------------------------------------------------------


async def test_linked_accounts(people):
    assert await sync.linked_slack_accounts(people.acme, people.alice) == [f"{TEAM}:UA"]
    assert await sync.linked_slack_accounts(people.acme, ERIN) == []


@pytest.mark.parametrize(
    ("oauth", "ok"),
    [
        (oauth_response(), True),
        (oauth_response(installer="UB"), False),  # someone else approved it in Slack
        (oauth_response(team="TOTHER"), False),  # alice's link is for another team
        (oauth_response(is_enterprise_install=True), False),
        (oauth_response() | {"access_token": None}, False),
    ],
    ids=["match", "other-installer", "other-team", "grid", "no-token"],
)
def test_verify_installer(oauth, ok):
    linked = [f"{TEAM}:UA"]
    if ok:
        sync.verify_installer(oauth, linked)
    else:
        with pytest.raises(sync.InstallVerificationError):
            sync.verify_installer(oauth, linked)


async def test_installation_token_encrypted_and_team_cannot_be_stolen(people):
    inst = await install(people, "TSTEAL")
    assert inst.token == "xoxb-sync-token" and inst.team_domain == "acme"
    async with worker_session(people.acme) as s:
        blob = await s.scalar(
            text("SELECT bot_token_encrypted FROM slack_installation WHERE team_id = 'TSTEAL'")
        )
    assert b"xoxb" not in bytes(blob)
    with pytest.raises(DBAPIError, match="row-level security"):
        await sync.upsert_installation(people.globex, oauth_response("TSTEAL"), {}, people.carol)
    async with worker_session() as s:
        ws = await s.scalar(text("SELECT app.workspace_for_slack_team('TSTEAL')"))
    assert ws == people.acme


# --- channels + ACL -------------------------------------------------------------------


def pure_channel(private: bool) -> sync.ChannelRef:
    return sync.ChannelRef("cid", TEAM, "C1", "#x", private, False, None)


def test_allowed_users_rules():
    users = {u["id"]: u for u in USERS}
    members = ["UA", "UD", "UX", "UBOT"]
    # Private: channel members only, minus deactivated users and bots.
    assert sync.allowed_slack_users(pure_channel(True), members, users) == {"UA", "UD"}
    # Public: plus every full member; the guest only via membership.
    assert sync.allowed_slack_users(pure_channel(False), ["UA"], users) == {"UA", "UB", "UC"}
    assert sync.allowed_slack_users(pure_channel(False), ["UD"], users) == {
        "UA", "UB", "UC", "UD"
    }  # fmt: skip


async def visible_channels(user_id: str, workspace_id: str) -> set[str]:
    async with rls_session(user_id, workspace_id) as s:
        rows = await s.execute(
            text(
                "SELECT c.name FROM source_container c WHERE c.source = 'slack'"
                " AND app.can_read_container(c.id)"
            )
        )
        return set(rows.scalars())


async def test_channel_and_acl_sync_end_to_end(people, slack_api):
    api = slack_api
    inst = await install(people)
    client = sync.client_for(inst)
    api.on(
        "users.conversations",
        {
            "channels": [
                {"id": "CPUB", "name": "general", "is_private": False},
                {"id": "CPRIV", "name": "leads", "is_private": True},
                {"id": "CEXT", "name": "partner", "is_private": False, "is_ext_shared": True},
            ]
        },
    )
    pending = await sync.sync_channels(people.acme, inst, client)
    channels = {c.name: c for c in await sync.active_channels(people.acme, TEAM)}
    assert set(channels) == {"#general", "#leads"}  # Slack Connect channel skipped
    assert len(pending) == 2
    assert api.calls_to("users.conversations")[0].token == "xoxb-sync-token"

    api.on("users.list", {"members": USERS})
    members = {"CPUB": ["UA"], "CPRIV": ["UA", "UC", "UD"]}
    api.on_call("conversations.members", lambda p: {"members": members[p["channel"]]})
    await sync.sync_team_acl(people.acme, TEAM, client)

    # Public channel: every linked full member in Acme (alice, bob); not the guest
    # (dave isn't in #general), not carol (not in Acme), not erin (no link).
    assert await visible_channels(people.alice, people.acme) == {"#general", "#leads"}
    assert await visible_channels(people.bob, people.acme) == {"#general"}
    assert await visible_channels(DAVE, people.acme) == {"#leads"}
    assert await visible_channels(ERIN, people.acme) == set()
    assert await visible_channels(people.carol, people.globex) == set()

    # Directory was refreshed for mention rendering.
    d = await sync.load_directory(people.acme, TEAM)
    assert d.users["UB"] == "bob" and "UBOT" in d.bots

    # Dave leaves #leads: his access goes at once, before any resync.
    await sync.revoke_member_access(people.acme, TEAM, "CPRIV", "UD")
    assert await visible_channels(DAVE, people.acme) == set()

    # A Slack error mid-sync writes nothing (alice keeps access until it goes stale).
    api.responses["users.list"] = [(200, {"ok": False, "error": "internal_error"}, {})]
    with pytest.raises(Exception):  # noqa: B017
        await sync.sync_team_acl(people.acme, TEAM, client)
    assert await visible_channels(people.alice, people.acme) == {"#general", "#leads"}

    # The bot is removed from #leads: access and content disappear at once.
    api.on("users.conversations", {"channels": [{"id": "CPUB", "name": "general"}]})
    await sync.sync_channels(people.acme, inst, client)
    assert await visible_channels(people.alice, people.acme) == {"#general"}


async def test_private_channel_messages_never_reach_non_members(people, slack_api):
    """End to end through the real search path (tacit_api + RLS)."""
    inst = await install(people, "TLEAK")
    client = sync.client_for(inst)
    slack_api.on(
        "users.conversations",
        {"channels": [{"id": "CSECRET", "name": "acquisition", "is_private": True}]},
    )
    await sync.sync_channels(people.acme, inst, client)
    slack_api.on("users.list", {"members": [user("UA2", "alice"), user("UB2", "bob")]})
    slack_api.on("conversations.members", {"members": ["UA2"]})
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        now = datetime.now(UTC)
        for uid, slack in [(people.alice, "UA2"), (people.bob, "UB2")]:
            conn.execute(
                'INSERT INTO account (id, "accountId", "providerId", "userId", "createdAt",'
                " \"updatedAt\") VALUES (%s, %s, 'slack', %s, %s, %s)",
                (f"acc_leak_{uid}", f"TLEAK:{slack}", uid, now, now),
            )
    await sync.sync_team_acl(people.acme, "TLEAK", client)
    (ch,) = await sync.active_channels(people.acme, "TLEAK")
    msg = {"type": "message", "user": "UA2", "ts": "1700000000.1",
           "text": "Zanzibarcorp acquisition closes Friday"}  # fmt: skip
    await ingest.upsert_documents(
        people.acme, ch.container_id, [n.normalize_message(msg, ch.as_channel())]
    )
    await index_workspace(people.acme)

    async def search(user_id: str) -> list[str]:
        async with rls_session(user_id, people.acme) as s:
            return [h.url for h in await hybrid_search(s, "Zanzibarcorp acquisition")]

    alice_hits = await search(people.alice)
    assert any("CSECRET" in u for u in alice_hits)
    assert not any("CSECRET" in u for u in await search(people.bob))
    assert not any("CSECRET" in u for u in await search(ERIN))


async def test_uninstall_wipes_token_and_hides_everything(people, slack_api):
    inst = await install(people, "TBYE")
    slack_api.on("users.conversations", {"channels": [{"id": "C1", "name": "general"}]})
    await sync.sync_channels(people.acme, inst, sync.client_for(inst))
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO webhook_delivery (delivery_id, source, event, team_id, payload)"
            " VALUES ('slack:EvBye', 'slack', 'app_uninstalled', 'TBYE',"
            ' \'{"team_id": "TBYE", "event": {"type": "app_uninstalled"}}\')'
        )
    assert await handlers.process_delivery("slack:EvBye") == "done"
    assert await sync.load_installation(people.acme, "TBYE") is None
    async with worker_session(people.acme) as s:
        row = (
            await s.execute(
                text(
                    "SELECT i.bot_token_encrypted IS NULL AS wiped, c.is_active"
                    " FROM slack_installation i JOIN slack_channel sc ON sc.team_id = i.team_id"
                    " JOIN source_container c ON c.id = sc.container_id WHERE i.team_id = 'TBYE'"
                )
            )
        ).one()
        ws = await s.scalar(text("SELECT app.workspace_for_slack_team('TBYE')"))
    assert row.wiped and not row.is_active and ws is None
