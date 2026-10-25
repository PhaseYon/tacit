"""Backfill: checkpointed history pages, thread replies, idempotency, rate limits."""

import pytest
from sqlalchemy import text

from tacit_api.connectors.slack import backfill, client, sync
from tacit_api.db import dispose_engine, worker_session
from tacit_api.search.indexer import index_workspace

from .conftest import Seed
from .slack_mock import message, user

TEAM = "TBACK"
ROOT, R1, R2, SOLO, JOIN = "1700000000.000100", "1700000010.0", "1700000020.0", "1700000100.0", "1700000200.0"  # fmt: skip


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


async def setup_channel(seed: Seed, slack_api, channel_id: str) -> tuple[sync.ChannelRef, object]:
    oauth = {"access_token": "xoxb-back", "bot_user_id": "UBOT",
             "team": {"id": TEAM, "name": "Back"}, "authed_user": {"id": "UA"}}  # fmt: skip
    await sync.upsert_installation(seed.acme, oauth, {"domain": "back"}, seed.alice)
    inst = await sync.load_installation(seed.acme, TEAM)
    ref = await sync.upsert_channel(seed.acme, inst, {"id": channel_id, "name": "eng"})
    await sync.upsert_directory(seed.acme, TEAM, [user("UA", "alice"), user("UB", "bob")])
    return ref, sync.client_for(inst)


def history_pages(api, newer: list[dict], older: list[dict]) -> None:
    """Two pages, newest first, like conversations.history."""
    api.on(
        "conversations.history",
        {"messages": newer, "has_more": True, "response_metadata": {"next_cursor": "page2"}},
        {"messages": older, "has_more": False, "response_metadata": {"next_cursor": ""}},
    )


async def docs(container_id: str) -> dict[str, tuple]:
    async with worker_session("org_acme") as s:
        rows = await s.execute(
            text(
                "SELECT d.metadata->>'ts', d.kind, p.metadata->>'ts', d.processed_hash"
                " IS NOT DISTINCT FROM d.content_hash FROM source_document d"
                " LEFT JOIN source_document p ON p.id = d.parent_id"
                " WHERE d.container_id = :c AND d.deleted_at IS NULL"
            ),
            {"c": container_id},
        )
        return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def cursor(container_id: str):
    async with worker_session("org_acme") as s:
        return (
            await s.execute(
                text(
                    "SELECT oldest_ts, next_cursor, run_latest_ts FROM slack_sync_cursor"
                    " WHERE container_id = :c"
                ),
                {"c": container_id},
            )
        ).one()


async def test_backfill_pages_threads_and_idempotency(seed, slack_api, monkeypatch):
    from tacit_api.config import get_settings

    monkeypatch.setenv("SLACK_BACKFILL_DAYS", "100000")  # fixture timestamps are from 2023
    get_settings.cache_clear()
    ch, c = await setup_channel(seed, slack_api, "CB1")
    root = message(ROOT, "Should we shard by tenant?", thread_ts=ROOT, reply_count=2,
                   latest_reply=R2)  # fmt: skip
    history_pages(
        slack_api,
        newer=[
            message(JOIN, "<@UB> has joined", "UB", subtype="channel_join"),
            message(SOLO, "lunch?"),
        ],
        older=[root],
    )
    slack_api.on(
        "conversations.replies",
        {"messages": [root, message(R1, "yes, by tenant", "UB", thread_ts=ROOT)],
         "response_metadata": {"next_cursor": "r2"}},
        {"messages": [message(R2, "agreed <@UB>", thread_ts=ROOT)],
         "response_metadata": {"next_cursor": ""}},
    )  # fmt: skip

    assert await backfill.backfill_page(seed.acme, ch, c) is True
    cur = await cursor(ch.container_id)
    assert cur.next_cursor == "page2" and cur.run_latest_ts == JOIN and cur.oldest_ts is None
    first = slack_api.calls_to("conversations.history")[0].params
    assert first["channel"] == "CB1" and first["limit"] == "200" and "cursor" not in first

    assert await backfill.backfill_page(seed.acme, ch, c) is False
    assert slack_api.calls_to("conversations.history")[1].params["cursor"] == "page2"
    cur = await cursor(ch.container_id)
    assert cur.next_cursor is None and cur.oldest_ts == JOIN  # watermark = newest seen

    got = await docs(ch.container_id)
    assert set(got) == {ROOT, R1, R2, SOLO}  # join message skipped
    assert got[ROOT][:2] == ("slack_message", None)
    assert got[R1][:2] == ("slack_reply", ROOT) and got[R2][:2] == ("slack_reply", ROOT)
    assert len(slack_api.calls_to("conversations.replies")) == 2  # both reply pages

    await index_workspace(seed.acme)
    assert all(processed for _, _, processed in (await docs(ch.container_id)).values())

    # Second run (reconcile): history from watermark minus overlap, same content.
    slack_api.on("conversations.history", {"messages": [message(SOLO, "lunch?"), root]})
    replies_before = len(slack_api.calls_to("conversations.replies"))
    assert await backfill.backfill_page(seed.acme, ch, c, reconcile=True) is False
    oldest = float(slack_api.calls_to("conversations.history")[-1].params["oldest"])
    assert oldest == pytest.approx(float(JOIN) - backfill.RECONCILE_OVERLAP.total_seconds())
    # Thread unchanged (same latest_reply): no replies call; nothing to re-embed.
    assert len(slack_api.calls_to("conversations.replies")) == replies_before
    assert (await index_workspace(seed.acme)).documents == 0
    assert all(processed for _, _, processed in (await docs(ch.container_id)).values())

    # A new reply moves latest_reply: only then is the thread re-fetched.
    r3 = "1700000300.0"
    slack_api.on(
        "conversations.history", {"messages": [root | {"latest_reply": r3, "reply_count": 3}]}
    )
    slack_api.on(
        "conversations.replies", {"messages": [root, message(r3, "ship it", thread_ts=ROOT)]}
    )
    await backfill.backfill_page(seed.acme, ch, c, reconcile=True)
    assert len(slack_api.calls_to("conversations.replies")) == replies_before + 1
    assert (await docs(ch.container_id))[r3][:2] == ("slack_reply", ROOT)


async def test_first_backfill_respects_horizon(seed, slack_api, monkeypatch):
    import time

    ch, c = await setup_channel(seed, slack_api, "CB2")
    slack_api.on("conversations.history", {"messages": []})
    await backfill.backfill_page(seed.acme, ch, c)
    oldest = float(slack_api.calls_to("conversations.history")[0].params["oldest"])
    assert oldest == pytest.approx(time.time() - 365 * 86400, abs=60)


async def test_long_rate_limit_surfaces_for_celery_retry(seed, slack_api):
    ch, c = await setup_channel(seed, slack_api, "CB3")
    slack_api.on(
        "conversations.history",
        {"error": "ratelimited"},
        status=429,
        headers={"Retry-After": "120"},
    )
    with pytest.raises(client.SlackError) as exc:
        await backfill.backfill_page(seed.acme, ch, c)
    assert client.rate_limit_wait(exc.value) == 120
    assert len(slack_api.calls_to("conversations.history")) == 1  # no in-process wait


async def test_short_rate_limit_is_retried_in_process(seed, slack_api):
    ch, c = await setup_channel(seed, slack_api, "CB4")
    slack_api.responses["conversations.history"] = [
        (429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "0"}),
        (200, {"ok": True, "messages": [message("1700000400.0", "after the wait")]}, {}),
    ]
    assert await backfill.backfill_page(seed.acme, ch, c) is False
    assert len(slack_api.calls_to("conversations.history")) == 2
    assert "1700000400.0" in await docs(ch.container_id)
