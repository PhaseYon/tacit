"""SlackSource against the fake Slack API: resolve, grouping, cursors, access, end to end."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tacit.config import Config, SourcesConfig
from tacit.sources.base import ScopeEntry, SourceAccessDenied
from tacit.sources.slack.source import LOOKBACK, SlackSource
from tacit.store import Store
from tacit.store import docs as d
from tacit.store import state as st
from tacit.sync import run_sync

from .slack_mock import message, slack_api, user  # noqa: F401  (fixture)

DAY0 = (datetime.now(UTC) - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
ENTRY = ScopeEntry("slack:C0ENG0001", "slack", "C0ENG0001", "#eng", {"private": False})
CHANNELS = [
    {"id": "C0GEN0001", "name": "general", "is_private": False},
    {"id": "C0ENG0001", "name": "eng", "is_private": False},
    {"id": "G0SEC0001", "name": "secret", "is_private": True},
]


def ts(day: int, hour: int, minute: int = 0) -> str:
    return f"{(DAY0 + timedelta(days=day, hours=hour, minutes=minute)).timestamp():.6f}"


def make_source(fake) -> SlackSource:
    return SlackSource("xoxp-test", Config(sources=SourcesConfig(slack_api_url=fake.url)))


class World:
    """A channel's history + threads, answered like Slack does (filtered by `oldest`)."""

    def __init__(self, fake) -> None:
        self.history: list[dict] = []
        self.replies: dict[str, list[dict]] = {}
        self.history_error: str | None = None
        fake.on(
            "users.list",
            {
                "members": [
                    user("UA", "alice", profile={"email": "alice@acme.test"}),
                    user("UB", "bob"),
                    user("UBOT", "deploybot", is_bot=True),
                ]
            },
        )
        fake.on("auth.test", {"url": "https://acme.slack.com/", "team_id": "T1"})
        fake.on_call("conversations.history", self._history)
        fake.on_call("conversations.replies", self._replies)

    def _history(self, params):
        if self.history_error:
            return {"ok": False, "error": self.history_error}
        oldest = float(params.get("oldest") or 0)
        msgs = [m for m in self.history if float(m["ts"]) >= oldest]
        return {"messages": sorted(msgs, key=lambda m: -float(m["ts"])), "has_more": False}

    def _replies(self, params):
        return {"messages": self.replies[params["ts"]], "has_more": False}

    def thread(self, parent: dict, *replies: dict) -> None:
        parent |= {"thread_ts": parent["ts"], "reply_count": len(replies)}
        self.history.append(parent)
        self.replies[parent["ts"]] = [parent, *(r | {"thread_ts": parent["ts"]} for r in replies)]


@pytest.fixture
def world(slack_api):  # noqa: F811
    w = World(slack_api)
    w.thread(
        message(ts(0, 9), "Should we move the cache to Redis?\nDetails below.", "UA"),
        message(ts(0, 10), "yes <@UA>, Postgres is too slow for this", "UB"),
        message(ts(0, 11), "build passed", "UBOT", bot_id="B1"),
    )
    # A reply broadcast to the channel shows up in history too; it belongs to the thread.
    w.history.append(
        message(ts(0, 10, 5), "also posting", "UB", subtype="thread_broadcast", thread_ts=ts(0, 9))
    )
    w.history += [
        message(ts(0, 12), "lunch?", "UA"),
        message(ts(0, 13), "sure", "UB"),
        message(ts(0, 14), "<@UB> has joined the channel", "UB", subtype="channel_join"),
        message(ts(0, 15), "deploy done", "UBOT", subtype="bot_message", bot_id="B1"),
        message(ts(1, 9), "standup: shipping auth", "UA"),
        message(ts(1, 9, 5), "standup: on billing", "UB"),
        message(ts(1, 9, 10), "standup: reviews", "UA"),
    ]
    return w


async def collect(src: SlackSource, entry: ScopeEntry, cursor: str | None):
    pages = [p async for p in src.fetch(entry, cursor)]
    return [i for p in pages for i in p.items], pages


# ---- resolve ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["#eng", "eng", "#ENG"])
async def test_resolve_by_name_across_pages(slack_api, target):  # noqa: F811
    slack_api.on(
        "conversations.list",
        {"channels": CHANNELS[:1], "response_metadata": {"next_cursor": "p2"}},
        {"channels": CHANNELS[1:], "response_metadata": {"next_cursor": ""}},
    )
    entry = await make_source(slack_api).resolve(target)
    assert entry == ScopeEntry("slack:C0ENG0001", "slack", "C0ENG0001", "#eng", {"private": False})
    assert slack_api.calls_to("conversations.list")[0].token == "xoxp-test"


async def test_resolve_by_id_and_private_flag(slack_api):  # noqa: F811
    slack_api.on("conversations.info", {"channel": CHANNELS[2]})
    entry = await make_source(slack_api).resolve("G0SEC0001")
    assert entry.label == "#secret" and entry.options == {"private": True}
    assert slack_api.calls_to("conversations.info")[0].params["channel"] == "G0SEC0001"


async def test_resolve_refuses_dms_and_group_dms(slack_api):  # noqa: F811
    src = make_source(slack_api)
    with pytest.raises(ValueError, match="never syncable"):
        await src.resolve("D0123ABCD")
    assert not slack_api.calls  # refused before asking Slack
    slack_api.on("conversations.info", {"channel": {"id": "G0MPIM001", "is_mpim": True}})
    with pytest.raises(ValueError, match="never syncable"):
        await src.resolve("G0MPIM001")


async def test_resolve_unknown_channel(slack_api):  # noqa: F811
    slack_api.on("conversations.list", {"channels": CHANNELS})
    with pytest.raises(ValueError, match="No Slack channel #nope"):
        await make_source(slack_api).resolve("#nope")
    slack_api.on("conversations.info", {"ok": False, "error": "channel_not_found"})
    with pytest.raises(ValueError, match="channel_not_found"):
        await make_source(slack_api).resolve("C0MISSING1")


# ---- fetch -----------------------------------------------------------------------------


async def test_threads_and_days_are_grouped_and_bots_skipped(world, slack_api):  # noqa: F811
    items, _ = await collect(make_source(slack_api), ENTRY, None)
    by_id = {i.external_id: i for i in items}
    day0 = DAY0.date().isoformat()
    day1 = (DAY0 + timedelta(days=1)).date().isoformat()
    assert set(by_id) == {
        f"slack:C0ENG0001:{ts(0, 9)}",
        f"slack:C0ENG0001:day:{day0}",
        f"slack:C0ENG0001:day:{day1}",
    }

    thread = by_id[f"slack:C0ENG0001:{ts(0, 9)}"]
    assert thread.kind == "slack_thread" and thread.source == "slack"
    assert thread.scope_key == "slack:C0ENG0001"
    assert thread.title == "Should we move the cache to Redis?"
    assert thread.url == f"https://acme.slack.com/archives/C0ENG0001/p{ts(0, 9).replace('.', '')}"
    hm = lambda h, m=0: (DAY0 + timedelta(hours=h, minutes=m)).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    assert thread.text == (
        f"@alice ({hm(9)}): Should we move the cache to Redis?\nDetails below.\n"
        f"@bob ({hm(10)}): yes @alice, Postgres is too slow for this"
    )
    assert "build passed" not in thread.text
    assert thread.participants == ("slack:UA", "slack:UB")
    assert thread.metadata["handles"] == {"slack:UA": "alice", "slack:UB": "bob"}
    assert thread.metadata["emails"] == {"slack:UA": "alice@acme.test"}
    assert thread.reference_time == DAY0 + timedelta(hours=10)
    assert thread.extract is True  # 2 human messages

    d0 = by_id[f"slack:C0ENG0001:day:{day0}"]
    assert d0.kind == "slack_day" and d0.title == f"#eng on {day0}"
    assert d0.text == f"@alice ({hm(12)}): lunch?\n@bob ({hm(13)}): sure"
    assert "also posting" not in d0.text  # broadcast reply lives in its thread
    assert d0.url.endswith(f"p{ts(0, 12).replace('.', '')}")
    assert d0.extract is False  # only 2 human messages
    assert d0.metadata["date"] == day0

    d1 = by_id[f"slack:C0ENG0001:day:{day1}"]
    assert d1.extract is True and d1.metadata["message_count"] == 3
    assert d1.reference_time == DAY0 + timedelta(days=1, hours=9, minutes=10)


async def test_thread_with_single_human_message_is_not_extracted(slack_api):  # noqa: F811
    w = World(slack_api)
    w.thread(
        message(ts(0, 9), "nightly report", "UBOT", subtype="bot_message", bot_id="B1"),
        message(ts(0, 10), "looks fine to me", "UB"),
    )
    items, _ = await collect(make_source(slack_api), ENTRY, None)
    assert len(items) == 1
    assert items[0].title == "looks fine to me" and items[0].extract is False


async def test_cursor_and_lookback(world, slack_api):  # noqa: F811
    world.history.append(message(ts(-20, 9), "old news", "UA"))
    src = make_source(slack_api)
    items, pages = await collect(src, ENTRY, None)
    assert len(items) == 4  # first sync backfills, old day included
    first_oldest = float(slack_api.calls_to("conversations.history")[0].params["oldest"])
    backfill_start = datetime.now(UTC) - timedelta(days=365)
    midnight = backfill_start.replace(hour=0, minute=0, second=0, microsecond=0)
    assert first_oldest == midnight.timestamp()
    cursor = pages[-1].cursor
    assert json.loads(cursor) == {"latest_ts": ts(1, 9, 10)}

    # Next run re-reads LOOKBACK before the cursor, from UTC midnight.
    items, _ = await collect(src, ENTRY, cursor)
    oldest = float(slack_api.calls_to("conversations.history")[-1].params["oldest"])
    want = (datetime.fromtimestamp(float(ts(1, 9, 10)), UTC) - LOOKBACK).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert oldest == want.timestamp()
    # Recent threads/days are re-read (hash skip makes it free); the old day is not.
    assert len(items) == 3 and all("old news" not in i.text for i in items)


async def test_pages_carry_monotonic_cursors(slack_api, monkeypatch):  # noqa: F811
    import tacit.sources.slack.source as mod

    monkeypatch.setattr(mod, "PAGE_ITEMS", 1)
    w = World(slack_api)
    w.history += [message(ts(0, 9), "a"), message(ts(1, 9), "b"), message(ts(2, 9), "c")]
    _, pages = await collect(make_source(slack_api), ENTRY, None)
    cursors = [json.loads(p.cursor)["latest_ts"] for p in pages]
    assert cursors == [ts(0, 9), ts(1, 9), ts(2, 9)]


async def test_empty_channel_keeps_cursor(slack_api):  # noqa: F811
    World(slack_api)
    items, pages = await collect(make_source(slack_api), ENTRY, None)
    assert items == [] and pages == []


@pytest.mark.parametrize("code", ["not_in_channel", "channel_not_found", "missing_scope"])
async def test_access_denied(world, slack_api, code):  # noqa: F811
    world.history_error = code
    with pytest.raises(SourceAccessDenied, match=code):
        await collect(make_source(slack_api), ENTRY, None)


async def test_other_errors_propagate(world, slack_api):  # noqa: F811
    from tacit.sources.slack.client import SlackError

    world.history_error = "internal_error"
    with pytest.raises(SlackError):
        await collect(make_source(slack_api), ENTRY, None)


# ---- end to end ------------------------------------------------------------------------


async def test_run_sync_end_to_end(store: Store, world, slack_api):  # noqa: F811
    await st.scope_add(store, ENTRY)
    r1 = await run_sync(store, {"slack": make_source(slack_api)})
    assert r1.scopes[0].error is None and r1.scopes[0].skipped_reason is None
    assert r1.scopes[0].written == 3
    assert json.loads(await st.cursor_get(store, ENTRY.key)) == {"latest_ts": ts(1, 9, 10)}
    doc = await d.get_doc(store, f"slack:C0ENG0001:{ts(0, 9)}")
    assert doc["url"].startswith("https://acme.slack.com/archives/C0ENG0001/")

    r2 = await run_sync(store, {"slack": make_source(slack_api)})
    assert r2.scopes[0].written == 0 and r2.scopes[0].unchanged == 3

    # A new reply on a recent thread is picked up through the lookback window.
    world.replies[ts(0, 9)].append(message(ts(1, 12), "done, merged", "UA", thread_ts=ts(0, 9)))
    r3 = await run_sync(store, {"slack": make_source(slack_api)})
    assert r3.scopes[0].written == 1 and r3.scopes[0].unchanged == 2


async def test_run_sync_skips_channel_without_access(store: Store, world, slack_api):  # noqa: F811
    await st.scope_add(store, ENTRY)
    world.history_error = "not_in_channel"
    r = await run_sync(store, {"slack": make_source(slack_api)})
    assert "not_in_channel" in r.scopes[0].skipped_reason
