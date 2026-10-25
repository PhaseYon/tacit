"""Webhook intake: signature checks, dedupe, enqueue, and delivery processing."""

import json
import uuid

import pytest
from githubkit.webhooks import sign
from sqlalchemy import text

from tacit_api.connectors.github import handlers, intake
from tacit_api.db import dispose_engine, worker_session

SECRET = "test-webhook-secret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    from tacit_api.config import get_settings

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def queued(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(intake, "enqueue_delivery", calls.append)
    return calls


def _headers(body: bytes, event: str = "issues", delivery: str | None = None, sig=None):
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
        "X-Hub-Signature-256": sig if sig is not None else sign(SECRET, body),
        "Content-Type": "application/json",
    }


BODY = json.dumps({"action": "opened", "installation": {"id": 777}}).encode()


async def test_valid_delivery_is_stored_and_queued(client, queued):
    h = _headers(BODY)
    r = await client.post("/webhooks/github", content=BODY, headers=h)
    assert r.status_code == 202
    assert queued == [h["X-GitHub-Delivery"]]
    async with worker_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT event, action, installation_id, status FROM webhook_delivery"
                    " WHERE delivery_id = :id"
                ),
                {"id": h["X-GitHub-Delivery"]},
            )
        ).one()
    await dispose_engine()
    assert tuple(row) == ("issues", "opened", 777, "queued")


async def test_duplicate_delivery_is_dropped(client, queued):
    h = _headers(BODY)
    assert (await client.post("/webhooks/github", content=BODY, headers=h)).status_code == 202
    r = await client.post("/webhooks/github", content=BODY, headers=h)
    assert r.status_code == 200 and r.json() == {"duplicate": True}
    assert len(queued) == 1


@pytest.mark.parametrize(
    "sig",
    ["", "sha256=" + "0" * 64, sign(SECRET, BODY, "sha1"), sign("wrong-secret", BODY)],
    ids=["missing", "garbage", "sha1-only", "wrong-secret"],
)
async def test_bad_signatures_rejected(client, queued, sig):
    r = await client.post("/webhooks/github", content=BODY, headers=_headers(BODY, sig=sig))
    assert r.status_code == 401
    assert queued == []


async def test_tampered_body_rejected(client, queued):
    h = _headers(BODY)
    r = await client.post("/webhooks/github", content=BODY + b" ", headers=h)
    assert r.status_code == 401


async def test_ping(client, queued):
    body = b'{"zen": "hi"}'
    r = await client.post("/webhooks/github", content=body, headers=_headers(body, "ping"))
    assert r.status_code == 200 and queued == []


async def test_unlinked_installation_is_ignored(client, queued):
    h = _headers(BODY)
    await client.post("/webhooks/github", content=BODY, headers=h)
    assert await handlers.process_delivery(h["X-GitHub-Delivery"]) == "ignored"
    await dispose_engine()


async def test_handler_runs_with_resolved_workspace(client, queued, seed, monkeypatch):
    import psycopg

    from .conftest import MIGRATOR_URL

    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO github_installation (workspace_id, installation_id, account_id,"
            " account_login, account_type) VALUES (%s, 5151, 1, 'acme', 'Organization')"
            " ON CONFLICT DO NOTHING",
            (seed.acme,),
        )
    seen = []

    async def fake(workspace_id, payload):
        seen.append((workspace_id, payload["action"]))
        return "done"

    monkeypatch.setitem(handlers.HANDLERS, "issues", fake)
    indexed = []
    monkeypatch.setattr(handlers, "_index_soon", indexed.append)
    body = json.dumps({"action": "edited", "installation": {"id": 5151}}).encode()
    h = _headers(body)
    await client.post("/webhooks/github", content=body, headers=h)
    assert await handlers.process_delivery(h["X-GitHub-Delivery"]) == "done"
    assert await handlers.process_delivery(h["X-GitHub-Delivery"]) == "skipped"
    assert seen == [(seed.acme, "edited")]
    assert indexed == [seed.acme]  # content changed -> indexing queued
    await dispose_engine()


async def test_failed_handler_is_recorded(client, queued, seed, monkeypatch):
    async def boom(workspace_id, payload):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(handlers.HANDLERS, "issues", boom)
    body = json.dumps({"action": "x", "installation": {"id": 5151}}).encode()
    h = _headers(body)
    await client.post("/webhooks/github", content=body, headers=h)
    with pytest.raises(RuntimeError):
        await handlers.process_delivery(h["X-GitHub-Delivery"])
    async with worker_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, attempts, error FROM webhook_delivery WHERE delivery_id = :id"
                ),
                {"id": h["X-GitHub-Delivery"]},
            )
        ).one()
    await dispose_engine()
    assert row.status == "failed" and row.attempts == 1 and "kaboom" in row.error
