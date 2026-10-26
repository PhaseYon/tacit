"""Events API intake (signatures, replay, dedupe, url_verification), event processing
(messages, edits, deletes, threads) and the OAuth install callback."""

import base64
import json
import time
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy import text

from tacit_api.connectors.slack import handlers, routes, sync
from tacit_api.db import dispose_engine, worker_session

from .conftest import MIGRATOR_URL, Seed
from .slack_mock import SIGNING_SECRET, sign, user

TEAM = "TWEB"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    from tacit_api.config import get_settings

    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


@pytest.fixture
def queued(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(handlers, "enqueue_delivery", calls.append)
    return calls


def envelope(event: dict, team: str = TEAM, event_id: str | None = None) -> dict:
    return {
        "type": "event_callback",
        "team_id": team,
        "event_id": event_id or f"Ev{uuid.uuid4().hex[:10]}",
        "event": event,
    }


# --- intake ---------------------------------------------------------------------------


async def test_url_verification_needs_a_valid_signature(client, queued):
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    r = await client.post("/webhooks/slack", content=body, headers=sign(body))
    assert r.status_code == 200 and r.json() == {"challenge": "abc123"}
    bad = sign(body, secret="wrong")
    assert (await client.post("/webhooks/slack", content=body, headers=bad)).status_code == 401


async def test_valid_event_is_stored_once_and_queued(client, queued):
    env = envelope({"type": "message", "channel": "C1", "user": "UA", "ts": "1.0", "text": "x"})
    body = json.dumps(env).encode()
    r = await client.post("/webhooks/slack", content=body, headers=sign(body))
    assert r.status_code == 200 and r.json() == {"queued": True}
    assert queued == [f"slack:{env['event_id']}"]
    async with worker_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT source, event, team_id, status FROM webhook_delivery"
                    " WHERE delivery_id = :id"
                ),
                {"id": queued[0]},
            )
        ).one()
    assert tuple(row) == ("slack", "message", TEAM, "queued")

    # Slack's retry (same event_id, fresh signature) and a replay are both dropped.
    retry = await client.post("/webhooks/slack", content=body, headers=sign(body))
    assert retry.json() == {"duplicate": True}
    assert len(queued) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Slack-Request-Timestamp": str(int(time.time())), "X-Slack-Signature": "v0=00"},
        "stale",
        "future",
        "wrong-secret",
        "tampered",
        {"X-Slack-Request-Timestamp": "nope", "X-Slack-Signature": "v0=00"},
    ],
    ids=["missing", "garbage", "stale", "future", "wrong-secret", "tampered", "bad-ts"],
)
async def test_bad_or_stale_signatures_rejected(client, queued, headers):
    body = json.dumps(envelope({"type": "message"})).encode()
    if headers == "stale":  # a captured request replayed 6 minutes later
        headers = sign(body, ts=int(time.time()) - 360)
    elif headers == "future":
        headers = sign(body, ts=int(time.time()) + 360)
    elif headers == "wrong-secret":
        headers = sign(body, secret="other")
    elif headers == "tampered":
        headers = sign(body)
        body = body.replace(b"message", b"massage")
    r = await client.post("/webhooks/slack", content=body, headers=headers)
    assert r.status_code == 401
    assert queued == []


async def test_signature_check_fails_closed_without_secret(client, queued, monkeypatch):
    from tacit_api.config import get_settings

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    get_settings.cache_clear()
    body = b'{"type": "url_verification", "challenge": "x"}'
    assert (
        await client.post("/webhooks/slack", content=body, headers=sign(body))
    ).status_code == 401


# --- event processing -----------------------------------------------------------------


@pytest.fixture(scope="module")
async def channel(seed: Seed) -> str:
    oauth = {
        "access_token": "xoxb-web",
        "bot_user_id": "UBOT",
        "team": {"id": TEAM, "name": "Web"},
        "authed_user": {"id": "UA"},
    }
    await sync.upsert_installation(seed.acme, oauth, {"domain": "acmeweb"}, seed.alice)
    inst = await sync.load_installation(seed.acme, TEAM)
    ref = await sync.upsert_channel(seed.acme, inst, {"id": "C1", "name": "eng"})
    await sync.upsert_directory(seed.acme, TEAM, [user("UA", "alice"), user("UB", "bob")])
    await dispose_engine()
    return ref.container_id


async def deliver(event: dict, team: str = TEAM) -> str:
    env = envelope(event, team)
    did = await handlers.record_delivery(env)
    assert did is not None
    return await handlers.process_delivery(did)


async def doc(ext: str):
    async with worker_session("org_acme") as s:
        return (
            await s.execute(
                text(
                    "SELECT kind, body, title, url, parent_id, deleted_at, author_login,"
                    " content_hash FROM source_document WHERE source = 'slack'"
                    " AND external_id = :e"
                ),
                {"e": ext},
            )
        ).one_or_none()


@pytest.fixture(autouse=True)
def _no_celery(monkeypatch):
    from tacit_api.connectors.slack import tasks
    from tacit_api.search import tasks as search_tasks

    sent: list = []
    monkeypatch.setattr(search_tasks.index_workspace_task, "delay", lambda *a: sent.append(a))
    monkeypatch.setattr(tasks.sync_acl, "delay", lambda *a: sent.append(("acl", *a)))
    monkeypatch.setattr(tasks.sync_team, "delay", lambda *a: sent.append(("team", *a)))
    return sent


async def test_message_edit_reply_delete_lifecycle(channel):
    root = {"type": "message", "channel": "C1", "user": "UA", "ts": "1700000000.000100",
            "text": "Let's drop Redis for <@UB>'s queue idea"}  # fmt: skip
    assert await deliver(root) == "done"
    d = await doc(f"{TEAM}:C1:1700000000.000100")
    assert d.kind == "slack_message" and d.author_login == "alice"
    assert d.body == "Let's drop Redis for @bob's queue idea"
    assert d.url == "https://acmeweb.slack.com/archives/C1/p1700000000000100"

    reply = {"type": "message", "channel": "C1", "user": "UB", "ts": "1700000050.000200",
             "thread_ts": "1700000000.000100", "text": "agreed, Postgres queues"}  # fmt: skip
    assert await deliver(reply) == "done"
    r = await doc(f"{TEAM}:C1:1700000050.000200")
    async with worker_session("org_acme") as s:
        root_id = await s.scalar(
            text("SELECT id FROM source_document WHERE external_id = :e"),
            {"e": f"{TEAM}:C1:1700000000.000100"},
        )
    assert r.kind == "slack_reply" and r.parent_id == root_id

    edit = {"type": "message", "subtype": "message_changed", "channel": "C1",
            "event_ts": "1700000100.000000",
            "message": root | {"text": "Let's drop Redis", "edited": {"ts": "1700000100.0"}}}  # fmt: skip
    assert await deliver(edit) == "done"
    assert (await doc(f"{TEAM}:C1:1700000000.000100")).body == "Let's drop Redis"

    # A stale copy of the original (older timestamp) arriving late doesn't win.
    assert await deliver(root) == "done"
    assert (await doc(f"{TEAM}:C1:1700000000.000100")).body == "Let's drop Redis"

    delete = {"type": "message", "subtype": "message_deleted", "channel": "C1",
              "deleted_ts": "1700000050.000200", "event_ts": "1700000200.0"}  # fmt: skip
    assert await deliver(delete) == "done"
    assert (await doc(f"{TEAM}:C1:1700000050.000200")).deleted_at is not None
    # An older edit replayed after the delete can't resurrect it.
    old_edit = {"type": "message", "subtype": "message_changed", "channel": "C1",
                "message": reply | {"edited": {"ts": "1700000150.0"}}}  # fmt: skip
    await deliver(old_edit)
    assert (await doc(f"{TEAM}:C1:1700000050.000200")).deleted_at is not None

    # Thread root deleted while replies remain: Slack sends a "tombstone" edit.
    tomb = {"type": "message", "subtype": "message_changed", "channel": "C1",
            "event_ts": "1700000300.0",
            "message": {"type": "message", "subtype": "tombstone", "ts": "1700000000.000100",
                        "text": "This message was deleted."}}  # fmt: skip
    assert await deliver(tomb) == "done"
    assert (await doc(f"{TEAM}:C1:1700000000.000100")).deleted_at is not None


@pytest.mark.parametrize(
    "event",
    [
        {"type": "message", "channel": "C1", "subtype": "channel_join", "user": "UB", "ts": "9.1",
         "text": "<@UB> has joined the channel"},
        {"type": "message", "channel": "C1", "subtype": "bot_message", "bot_id": "B1",
         "ts": "9.2", "text": "deploy ok"},
        {"type": "message", "channel": "CUNKNOWN", "user": "UA", "ts": "9.3", "text": "hi"},
        {"type": "reaction_added", "user": "UA"},
    ],
    ids=["join", "bot", "unsynced-channel", "unhandled-event"],
)  # fmt: skip
async def test_ignored_events(channel, event):
    assert await deliver(event) == "ignored"


async def test_unknown_team_is_ignored(channel):
    msg = {"type": "message", "channel": "C1", "user": "UA", "ts": "9.9", "text": "hi"}
    assert await deliver(msg, team="TNOBODY") == "ignored"
    assert await doc("TNOBODY:C1:9.9") is None


async def test_member_left_and_user_deactivated_narrow_access_first(seed, channel, _no_celery):
    now = datetime.now(UTC)
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO container_access (container_id, user_id, workspace_id, permission,"
            " via_external_id) VALUES (%s, %s, %s, 'member', %s), (%s, %s, %s, 'member', %s)"
            " ON CONFLICT DO NOTHING",
            (
                channel,
                seed.alice,
                seed.acme,
                f"{TEAM}:UA",
                channel,
                seed.bob,
                seed.acme,
                f"{TEAM}:UB",
            ),
        )
        conn.execute("UPDATE source_container SET acl_synced_at = %s WHERE id = %s", (now, channel))

    async def holders() -> set[str]:
        async with worker_session(seed.acme) as s:
            rows = await s.execute(
                text("SELECT user_id FROM container_access WHERE container_id = :c"),
                {"c": channel},
            )
            return set(rows.scalars())

    assert await deliver({"type": "member_left_channel", "channel": "C1", "user": "UB"}) == "done"
    assert await holders() == {seed.alice}
    assert ("acl", seed.acme, TEAM) in _no_celery
    deact = {"type": "user_change", "user": {"id": "UA", "deleted": True}}
    assert await deliver(deact) == "done"
    assert await holders() == set()


# --- install callback -----------------------------------------------------------------


async def test_oauth_callback_installs_and_rejects_other_installer(
    client, seed, slack_api, monkeypatch
):
    from tacit_api.connectors.slack import tasks

    started: list = []
    monkeypatch.setattr(tasks.sync_team, "delay", lambda *a: started.append(a))
    now = datetime.now(UTC)
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            'INSERT INTO account (id, "accountId", "providerId", "userId", "createdAt",'
            " \"updatedAt\") VALUES ('acc_cb', 'TCB:UA', 'slack', %s, %s, %s)",
            (seed.alice, now, now),
        )
    oauth = {"access_token": "xoxb-cb", "bot_user_id": "UBOT", "scope": "channels:read",
             "team": {"id": "TCB", "name": "CB"}, "authed_user": {"id": "UA"}}  # fmt: skip
    slack_api.on("oauth.v2.access", oauth)
    slack_api.on("team.info", {"team": {"id": "TCB", "domain": "cbteam"}})

    state = routes.make_state(seed.acme, seed.alice, "/w/acme")
    r = await client.get(f"/slack/oauth/callback?code=abc&state={state}")
    assert r.status_code == 307 and r.headers["location"].endswith("/w/acme?slack=connected")
    exchange = slack_api.calls_to("oauth.v2.access")[0]
    assert exchange.params["code"] == "abc"
    assert exchange.token == "Basic " + base64.b64encode(b"cid:csecret").decode()
    assert started == [(seed.acme, "TCB")]
    inst = await sync.load_installation(seed.acme, "TCB")
    assert inst.token == "xoxb-cb" and inst.team_domain == "cbteam"

    # Approved in Slack by someone other than the Tacit user who started it.
    slack_api.on("oauth.v2.access", oauth | {"authed_user": {"id": "UB"}})
    r = await client.get(f"/slack/oauth/callback?code=abc&state={state}")
    assert r.headers["location"].endswith("slack_error=not_allowed")

    r = await client.get("/slack/oauth/callback?code=abc&state=forged")
    assert r.headers["location"].endswith("slack_error=expired")


async def test_connect_requires_admin_and_linked_slack(client, seed, slack_api, make_token):
    bob = {"Authorization": f"Bearer {make_token(seed.bob)}"}
    r = await client.post(f"/workspaces/{seed.acme}/slack/connect", json={}, headers=bob)
    assert r.status_code == 403
    now = datetime.now(UTC)
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:  # an owner with no Slack link
        conn.execute(
            'INSERT INTO "user" (id, name, email, "emailVerified") VALUES'
            " ('u_nolink', 'No Link', 'nolink@example.com', true) ON CONFLICT DO NOTHING"
        )
        conn.execute(
            'INSERT INTO member (id, "organizationId", "userId", role, "createdAt") VALUES'
            " ('m_nolink', %s, 'u_nolink', 'admin', %s) ON CONFLICT DO NOTHING",
            (seed.acme, now),
        )
    nolink = {"Authorization": f"Bearer {make_token('u_nolink')}"}
    r = await client.post(f"/workspaces/{seed.acme}/slack/connect", json={}, headers=nolink)
    assert r.status_code == 409

    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            'INSERT INTO account (id, "accountId", "providerId", "userId", "createdAt",'
            " \"updatedAt\") VALUES ('acc_connect', 'TCONN:UA', 'slack', %s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (seed.alice, now, now),
        )
    alice = {"Authorization": f"Bearer {make_token(seed.alice)}"}
    r = await client.post(
        f"/workspaces/{seed.acme}/slack/connect", json={"return_to": "/w/acme"}, headers=alice
    )
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("https://slack.com/oauth/v2/authorize?")
    assert "channels%3Ahistory" in url and "im%3A" not in url and "chat%3Awrite" not in url
