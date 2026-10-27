"""Pure normalizer tests: mrkdwn conversion, skipping, threads, hashing, permalinks."""

import pytest

from tacit_api.connectors.slack import normalize as n

CH = n.Channel("T1", "C1", "#eng", "acme")
DIR = n.Directory(users={"UA": "alice", "UB": "bob"}, bots=frozenset({"UBOT"}))


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("hi <@UB>", "hi @bob"),
        ("hi <@UZ|zed>", "hi @zed"),
        ("hi <@UZ>", "hi @UZ"),
        ("see <#C9|design>", "see #design"),
        ("<!here> deploy", "@here deploy"),
        ("<!channel> <!everyone>", "@channel @everyone"),
        ("ping <!subteam^S1|@platform>", "ping @platform"),
        ("due <!date^1700000000^{date}|Nov 14>", "due Nov 14"),
        ("read <https://x.io/adr|the ADR>", "read the ADR (https://x.io/adr)"),
        ("raw <https://x.io>", "raw https://x.io"),
        ("mail <mailto:a@x.io|a@x.io>", "mail a@x.io"),
        ("a &lt; b &amp;&amp; c &gt; d", "a < b && c > d"),
        ("literal &amp;lt;", "literal &lt;"),
    ],
)
def test_mrkdwn_to_text(raw, want):
    assert n.mrkdwn_to_text(raw, DIR) == want


def test_top_level_message():
    msg = {
        "type": "message",
        "user": "UA",
        "ts": "1700000000.000100",
        "text": "We chose Postgres over Mongo <@UB>\nbecause joins.",
        "reactions": [{"name": "+1", "count": 3}],
    }
    d = n.normalize_message(msg, CH, DIR)
    assert d is not None
    assert d.kind == "slack_message"
    assert d.external_id == "T1:C1:1700000000.000100"
    assert d.url == "https://acme.slack.com/archives/C1/p1700000000000100"
    assert d.title == "We chose Postgres over Mongo @bob"
    assert d.body == "We chose Postgres over Mongo @bob\nbecause joins."
    assert d.author_external_id == "T1:UA" and d.author_login == "alice"
    assert d.parent_external_id is None
    assert d.metadata["channel"] == "#eng"


def test_reply_links_to_root_and_has_thread_permalink():
    msg = {
        "type": "message",
        "user": "UB",
        "ts": "1700000100.000200",
        "thread_ts": "1700000000.000100",
        "text": "agreed",
    }
    d = n.normalize_message(msg, CH, DIR)
    assert d.kind == "slack_reply"
    assert d.title is None
    assert d.parent_external_id == "T1:C1:1700000000.000100"
    assert d.url == (
        "https://acme.slack.com/archives/C1/p1700000100000200?thread_ts=1700000000.000100&cid=C1"
    )


def test_thread_root_is_a_message_with_reply_metadata():
    msg = {
        "type": "message",
        "user": "UA",
        "ts": "1.000001",
        "thread_ts": "1.000001",
        "reply_count": 2,
        "latest_reply": "3.000001",
        "text": "root",
    }
    d = n.normalize_message(msg, CH, DIR)
    assert d.kind == "slack_message" and d.parent_external_id is None
    assert d.metadata["reply_count"] == 2


@pytest.mark.parametrize(
    "msg",
    [
        {"subtype": "channel_join", "user": "UA", "text": "<@UA> has joined"},
        {"subtype": "channel_topic", "user": "UA", "text": "set the topic"},
        {"subtype": "bot_message", "bot_id": "B1", "text": "build passed"},
        {"user": "UA", "bot_id": "B1", "text": "app posting as a user"},
        {"user": "UBOT", "text": "a bot user"},
        {"user": "USLACKBOT", "text": "reminder"},
        {"user": "UA", "text": "   "},
        {"text": "no user"},
    ],
    ids=["join", "topic", "bot_message", "bot_id", "bot-user", "slackbot", "empty", "no-user"],
)
def test_skips_non_human_messages(msg):
    assert n.normalize_message({"type": "message", "ts": "1.0", **msg}, CH, DIR) is None


def test_thread_broadcast_and_file_share_are_ingested():
    b = {"type": "message", "subtype": "thread_broadcast", "user": "UA", "ts": "2.0",
         "thread_ts": "1.0", "text": "also posting to channel"}  # fmt: skip
    assert n.normalize_message(b, CH, DIR).kind == "slack_reply"
    f = {"type": "message", "subtype": "file_share", "user": "UA", "ts": "3.0", "text": "",
         "files": [{"name": "arch.png", "filetype": "png", "url_private": "https://x"}]}  # fmt: skip
    d = n.normalize_message(f, CH, DIR)
    assert d.body == "[Attached: arch.png]"
    assert d.metadata["files"] == [{"name": "arch.png", "filetype": "png"}]


def test_hash_ignores_reactions_and_edit_markers_but_not_text():
    base = {"type": "message", "user": "UA", "ts": "5.0", "text": "ship it"}
    h = n.normalize_message(base, CH, DIR).content_hash
    noisy = base | {"reactions": [{"name": "tada"}], "reply_count": 9, "edited": {"ts": "6.0"}}
    assert n.normalize_message(noisy, CH, DIR).content_hash == h
    edited = base | {"text": "ship it tomorrow", "edited": {"ts": "6.0"}}
    d = n.normalize_message(edited, CH, DIR)
    assert d.content_hash != h
    assert d.source_updated_at > d.source_created_at


def test_permalink_without_domain():
    ch = n.Channel("T1", "C1", "#x")
    assert n.permalink(ch, "1.5") == "https://slack.com/archives/C1/p15"


def test_member_classification():
    assert n.is_full_member(n_user := {"id": "U1"})
    assert not n.is_full_member(n_user | {"is_restricted": True})
    assert not n.is_full_member(n_user | {"is_ultra_restricted": True})
    assert not n.is_full_member(n_user | {"deleted": True})
    assert n.is_active_human(n_user | {"is_restricted": True})
    assert not n.is_active_human(n_user | {"is_bot": True})
    d = n.directory_from_users(
        [{"id": "U1", "name": "al"}, {"id": "U2", "name": "b", "is_bot": True}]
    )
    assert d.users == {"U1": "al", "U2": "b"} and d.bots == {"U2"}
