"""Webhook intake (signatures, verification handshake, dedupe), event processing
(refetch, delete, lost access) and the OAuth connect flow."""

import base64
import json
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import text

from tacit_api.connectors.notion import backfill, handlers, routes, sync
from tacit_api.db import dispose_engine, worker_session

from .conftest import Seed
from .notion_mock import block, page, sign

NWS = "NW-HOOK"


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


@pytest.fixture
def queued(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(handlers, "enqueue_delivery", calls.append)
    return calls


@pytest.fixture(autouse=True)
def _no_celery(monkeypatch):
    from tacit_api.connectors.notion import tasks
    from tacit_api.search import tasks as search_tasks

    sent: list = []
    monkeypatch.setattr(search_tasks.index_workspace_task, "delay", lambda *a: sent.append(a))
    monkeypatch.setattr(tasks.sync_connection, "delay", lambda *a: sent.append(("sync", *a)))
    return sent


def event(etype: str, entity_id: str, *, accessible=None, nws: str = NWS, **extra) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "timestamp": "2025-10-10T10:00:00.000Z",
        "workspace_id": nws,
        "subscription_id": "sub-1",
        "integration_id": "int-1",
        "type": etype,
        "authors": [{"id": "nu-alice", "type": "person"}],
        "accessible_by": accessible if accessible is not None else [],
        "attempt_number": 1,
        "entity": {"id": entity_id, "type": "page"},
        "data": {},
        **extra,
    }


# --- intake ---------------------------------------------------------------------------


async def test_signed_event_is_stored_once_and_queued(client, notion_api, queued):
    body = json.dumps(event("page.content_updated", "p-1")).encode()
    r = await client.post("/webhooks/notion", content=body, headers=sign(body))
    assert r.status_code == 200 and r.json() == {"queued": True}
    async with worker_session() as s:
        row = (
            await s.execute(
                text("SELECT source, event, status FROM webhook_delivery WHERE delivery_id = :id"),
                {"id": queued[0]},
            )
        ).one()
    assert tuple(row) == ("notion", "page.content_updated", "queued")
    # Notion's retry (same event id) and a replay are both dropped.
    again = await client.post("/webhooks/notion", content=body, headers=sign(body))
    assert again.json() == {"duplicate": True} and len(queued) == 1


@pytest.mark.parametrize("case", ["missing", "garbage", "wrong-secret", "tampered", "no-prefix"])
async def test_bad_signatures_rejected(client, notion_api, queued, case):
    body = json.dumps(event("page.created", "p-1")).encode()
    headers = {"Content-Type": "application/json"}
    if case == "garbage":
        headers["X-Notion-Signature"] = "sha256=00"
    elif case == "wrong-secret":
        headers = sign(body, secret="other")
    elif case == "tampered":
        headers = sign(body)
        body = body.replace(b"page.created", b"page.deleted")
    elif case == "no-prefix":
        headers = sign(body)
        headers["X-Notion-Signature"] = headers["X-Notion-Signature"].removeprefix("sha256=")
    r = await client.post("/webhooks/notion", content=body, headers=headers)
    assert r.status_code == 401 and queued == []


async def test_verification_handshake(client, notion_api, queued, monkeypatch):
    from tacit_api.config import get_settings

    handshake = json.dumps({"verification_token": "secret_handshake_1"}).encode()
    # Token already configured: an unsigned handshake is just an unsigned request.
    r = await client.post("/webhooks/notion", content=handshake)
    assert r.status_code == 401

    # Setup mode (no token yet): only the exact handshake shape is accepted and stored.
    monkeypatch.setenv("NOTION_WEBHOOK_VERIFICATION_TOKEN", "")
    get_settings.cache_clear()
    r = await client.post("/webhooks/notion", content=handshake)
    assert r.status_code == 200
    async with worker_session() as s:
        stored = await s.scalar(
            text(
                "SELECT payload->>'verification_token' FROM webhook_delivery"
                " WHERE event = 'verification' AND payload->>'verification_token' = :t"
            ),
            {"t": "secret_handshake_1"},
        )
    assert stored == "secret_handshake_1" and queued == []
    # Everything else still fails closed without a configured token.
    body = json.dumps(event("page.created", "p-1")).encode()
    assert (
        await client.post("/webhooks/notion", content=body, headers=sign(body))
    ).status_code == 401
    sneaky = json.dumps({"verification_token": "x", "type": "page.deleted"}).encode()
    assert (await client.post("/webhooks/notion", content=sneaky)).status_code == 401


# --- event processing -----------------------------------------------------------------


async def deliver(ev: dict) -> str:
    did = await handlers.record_delivery(ev)
    assert did is not None
    return await handlers.process_delivery(did)


async def doc(cid: str, page_id: str):
    async with worker_session("org_acme") as s:
        return (
            await s.execute(
                text(
                    "SELECT body, deleted_at FROM source_document WHERE source = 'notion'"
                    " AND external_id = :e"
                ),
                {"e": f"{cid}:{page_id}"},
            )
        ).one_or_none()


async def test_events_refetch_delete_and_lost_access(seed: Seed, notion_api):
    fake = notion_api
    fake.add(page("p-hook", "Hooked"))
    fake.children["p-hook"] = [block("paragraph", "first version")]
    oauth = {
        "access_token": "ntn_hook",
        "bot_id": "bot-hook",
        "workspace_id": NWS,
        "owner": {"type": "user", "user": {"id": "nu-alice", "name": "Alice"}},
    }
    cid = await sync.upsert_connection(seed.acme, seed.alice, oauth)

    # A new page: Notion says our bot can see it -> fetched with OUR token.
    assert (
        await deliver(
            event("page.created", "p-hook", accessible=[{"id": "bot-hook", "type": "bot"}])
        )
        == "done"
    )
    assert (await doc(cid, "p-hook")).body == "first version"
    assert fake.calls_to("pages/p-hook")[-1].token == "ntn_hook"

    # The payload is never trusted: content comes from the API.
    fake.objects["p-hook"]["last_edited_time"] = "2025-10-09T10:00:00.000Z"
    fake.children["p-hook"] = [block("paragraph", "second version")]
    ev = event("page.content_updated", "p-hook", data={"updated_blocks": [{"text": "evil"}]})
    assert await deliver(ev) == "done"
    assert (await doc(cid, "p-hook")).body == "second version"

    # Unshared: the refetch 404s, so this connection's copy is hidden.
    fake.access["ntn_hook"] = set()
    assert await deliver(event("page.properties_updated", "p-hook")) == "done"
    assert (await doc(cid, "p-hook")).deleted_at is not None

    # Undeleted + shared again -> back; then page.deleted tombstones without a fetch.
    fake.access.pop("ntn_hook")
    assert await deliver(event("page.undeleted", "p-hook")) == "done"
    assert (await doc(cid, "p-hook")).deleted_at is None
    calls = len(fake.calls)
    assert await deliver(event("page.deleted", "p-hook")) == "done"
    assert (await doc(cid, "p-hook")).deleted_at is not None and len(fake.calls) == calls

    # Another connection's bot only: not ours, and we don't hold the page -> ignored.
    other = event("page.created", "p-other", accessible=[{"id": "bot-someone", "type": "bot"}])
    assert await deliver(other) == "ignored"
    # Unknown Notion workspace, comments, locks: ignored.
    assert await deliver(event("page.created", "p-x", nws="NW-UNKNOWN")) == "ignored"
    assert await deliver(event("comment.created", "c-1")) == "ignored"
    assert await deliver(event("page.locked", "p-hook")) == "ignored"


async def test_database_events_schedule_incremental_sync(seed: Seed, notion_api, _no_celery):
    oauth = {
        "access_token": "ntn_db",
        "bot_id": "bot-db",
        "workspace_id": "NW-DB",
        "owner": {"type": "user", "user": {"id": "nu-alice"}},
    }
    cid = await sync.upsert_connection(seed.acme, seed.alice, oauth)
    ev = event("data_source.content_updated", "ds-1", nws="NW-DB")
    assert await deliver(ev) == "done"
    assert ("sync", seed.acme, cid) in _no_celery


async def test_rate_limit_surfaces_retry_after(seed: Seed, notion_api):
    from tacit_api.connectors.notion.client import rate_limit_wait

    fake = notion_api
    fake.failures["search"] = (
        429,
        {"object": "error", "status": 429, "code": "rate_limited", "message": "slow"},
        {"Retry-After": "0"},
    )
    oauth = {
        "access_token": "ntn_rl",
        "bot_id": "bot-rl",
        "workspace_id": "NW-RL",
        "owner": {"type": "user", "user": {"id": "nu-alice"}},
    }
    cid = await sync.upsert_connection(seed.acme, seed.alice, oauth)
    with pytest.raises(Exception) as info:  # noqa: B017
        await sync.with_connection(seed.acme, cid, lambda c, cl: backfill.run_page(c, cl))
    assert rate_limit_wait(info.value) is not None
    assert len(fake.calls_to("search")) == 3  # the SDK retried twice in-process


# --- connect flow ---------------------------------------------------------------------


async def test_connect_url_for_any_member(client, seed, notion_api, make_token, monkeypatch):
    bob = {"Authorization": f"Bearer {make_token(seed.bob)}"}
    r = await client.post(
        f"/workspaces/{seed.acme}/notion/connect", json={"return_to": "/w/acme"}, headers=bob
    )
    assert r.status_code == 200
    url = urlparse(r.json()["url"])
    q = parse_qs(url.query)
    assert url.netloc == "api.notion.com" and url.path == "/v1/oauth/authorize"
    assert q["owner"] == ["user"] and q["response_type"] == ["code"]
    assert routes.read_state(q["state"][0])["sub"] == seed.bob

    from tacit_api.config import get_settings

    monkeypatch.setenv("NOTION_CLIENT_ID", "")
    get_settings.cache_clear()
    r = await client.post(f"/workspaces/{seed.acme}/notion/connect", json={}, headers=bob)
    assert r.status_code == 503


async def test_oauth_callback_checks_state_and_owner(client, seed, notion_api, monkeypatch):
    from tacit_api.connectors.notion import tasks

    started: list = []
    monkeypatch.setattr(tasks.sync_connection, "delay", lambda *a: started.append(a))
    fake = notion_api
    fake.oauth = {
        "access_token": "ntn_cb",
        "refresh_token": "ref_cb",
        "bot_id": "bot-cb",
        "workspace_id": "NW-CB",
        "workspace_name": "CB",
        "owner": {"type": "user", "user": {"id": "nu-alice", "name": "Alice"}},
    }
    state = routes.make_state(seed.acme, seed.alice, "/w/acme")
    r = await client.get(f"/notion/oauth/callback?code=abc&state={state}")
    assert r.status_code == 307 and r.headers["location"].endswith("/w/acme?notion=connected")
    exchange = fake.calls_to("oauth/token")[0]
    assert exchange.body == {
        "grant_type": "authorization_code",
        "code": "abc",
        "redirect_uri": "http://localhost:8000/notion/oauth/callback",
    }
    assert exchange.auth == "Basic " + base64.b64encode(b"notion-cid:notion-csecret").decode()
    (cid_started,) = [a[1] for a in started]
    assert started == [(seed.acme, cid_started, "full")]
    conn = await sync.load_connection(seed.acme, cid_started)
    assert conn.user_id == seed.alice and conn.access_token == "ntn_cb"
    async with worker_session(seed.acme) as s:
        ident = await s.scalar(
            text("SELECT login FROM external_identity WHERE external_id = 'NW-CB:nu-alice'")
        )
    assert ident == "Alice"

    # A workspace-owned token has no person to scope visibility to: refused.
    fake.oauth = fake.oauth | {
        "bot_id": "bot-cb2",
        "owner": {"type": "workspace", "workspace": True},
    }
    r = await client.get(f"/notion/oauth/callback?code=abc&state={state}")
    assert r.headers["location"].endswith("notion_error=not_allowed")

    # Forged / expired state, user cancelled, and a bot linked to another workspace.
    r = await client.get("/notion/oauth/callback?code=abc&state=forged")
    assert r.headers["location"].endswith("notion_error=expired")
    r = await client.get(f"/notion/oauth/callback?error=access_denied&state={state}")
    assert r.headers["location"].endswith("notion_error=cancelled")
    fake.oauth = fake.oauth | {
        "bot_id": "bot-cb",
        "owner": {"type": "user", "user": {"id": "nu-carol"}},
    }
    other = routes.make_state(seed.globex, seed.carol, "/w/globex")
    r = await client.get(f"/notion/oauth/callback?code=abc&state={other}")
    assert r.headers["location"].endswith("notion_error=taken")
