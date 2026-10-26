"""Pure functions: Slack message dict -> NormalizedDocument. No I/O.

The same normalizer handles conversations.history / conversations.replies items and
Events API message payloads, so backfill and webhooks produce identical rows.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..documents import NormalizedDocument, normalize_body

# Subtypes that carry something a person wrote. Everything else (joins, leaves, topic
# and name changes, pins, bot messages, channel system notices) is skipped.
HUMAN_SUBTYPES = {None, "thread_broadcast", "file_share", "me_message"}
TITLE_MAX = 120
SLACKBOT = "USLACKBOT"


@dataclass(frozen=True)
class Channel:
    team_id: str
    channel_id: str
    name: str
    team_domain: str | None = None

    def external_id(self, ts: str) -> str:
        return f"{self.team_id}:{self.channel_id}:{ts}"


@dataclass(frozen=True)
class Directory:
    """Slack user id -> handle, plus which ids are bots. Used to render mentions."""

    users: dict[str, str] = field(default_factory=dict)
    bots: frozenset[str] = frozenset()
    channels: dict[str, str] = field(default_factory=dict)


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), UTC)


def permalink(ch: Channel, ts: str, thread_ts: str | None = None) -> str:
    host = f"https://{ch.team_domain}.slack.com" if ch.team_domain else "https://slack.com"
    url = f"{host}/archives/{ch.channel_id}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={ch.channel_id}"
    return url


_TOKEN = re.compile(r"<([^<>]+)>")
_SPECIAL = {"here": "@here", "channel": "@channel", "everyone": "@everyone"}


def mrkdwn_to_text(text: str | None, directory: Directory | None = None) -> str:
    """Slack's escaped mrkdwn -> readable plain text (mentions, channels, links)."""
    if not text:
        return ""
    d = directory or Directory()

    def repl(m: re.Match[str]) -> str:
        inner = m.group(1)
        target, _, label = inner.partition("|")
        if target.startswith("@"):
            uid = target[1:]
            return "@" + (d.users.get(uid) or label.lstrip("@") or uid)
        if target.startswith("#"):
            cid = target[1:]
            return "#" + (label or d.channels.get(cid) or cid)
        if target.startswith("!"):
            word = target[1:]
            if word in _SPECIAL:
                return _SPECIAL[word]
            if word.startswith("subteam^"):
                return label if label.startswith("@") else "@" + (label or "group")
            return label or ""  # !date^…|fallback and unknown commands
        if target.startswith("mailto:"):
            return label or target[len("mailto:") :]
        if label and label != target:
            return f"{label} ({target})"
        return target

    out = _TOKEN.sub(repl, text)
    # Slack escapes only these three; unescape after link parsing (& last).
    return out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def is_human_message(msg: dict[str, Any], directory: Directory | None = None) -> bool:
    if msg.get("type", "message") != "message":
        return False
    if msg.get("subtype") not in HUMAN_SUBTYPES:
        return False
    if msg.get("bot_id") or msg.get("bot_profile"):
        return False
    user = msg.get("user")
    if not user or user == SLACKBOT:
        return False
    return not (directory and user in directory.bots)


def _files(msg: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": f.get("name") or f.get("title"), "filetype": f.get("filetype")}
        for f in msg.get("files") or []
        if f.get("name") or f.get("title")
    ]


def normalize_message(
    msg: dict[str, Any], ch: Channel, directory: Directory | None = None
) -> NormalizedDocument | None:
    """One message -> one document, or None if it isn't something a person wrote."""
    if not is_human_message(msg, directory):
        return None
    ts = msg["ts"]
    thread_ts = msg.get("thread_ts")
    is_reply = bool(thread_ts and thread_ts != ts)
    files = _files(msg)

    text = mrkdwn_to_text(msg.get("text"), directory)
    if files:
        names = ", ".join(f["name"] for f in files)
        text = f"{text}\n\n[Attached: {names}]" if text.strip() else f"[Attached: {names}]"
    body, truncated = normalize_body(text)
    if not body:
        return None

    title = None
    if not is_reply:
        first = next((line.strip() for line in body.split("\n") if line.strip()), "")
        title = first if len(first) <= TITLE_MAX else first[: TITLE_MAX - 1] + "…"

    user = msg["user"]
    handle = (directory.users.get(user) if directory else None) or msg.get("user_name")
    edited_ts = (msg.get("edited") or {}).get("ts")
    metadata: dict[str, Any] = {
        "channel": ch.name,
        "channel_id": ch.channel_id,
        "ts": ts,
        "thread_ts": thread_ts if thread_ts else None,
        "subtype": msg.get("subtype"),
    }
    if not is_reply and thread_ts:
        metadata["reply_count"] = msg.get("reply_count", 0)
        metadata["latest_reply"] = msg.get("latest_reply")
    if edited_ts:
        metadata["edited_ts"] = edited_ts
    if files:
        metadata["files"] = files
    if truncated:
        metadata["truncated"] = True

    return NormalizedDocument(
        kind="slack_reply" if is_reply else "slack_message",
        external_id=ch.external_id(ts),
        url=permalink(ch, ts, thread_ts if is_reply else None),
        title=title,
        body=body,
        author_external_id=f"{ch.team_id}:{user}",
        author_login=handle,
        source_created_at=parse_ts(ts),
        source_updated_at=parse_ts(edited_ts) or parse_ts(ts),
        parent_external_id=ch.external_id(thread_ts) if is_reply else None,
        metadata=metadata,
        # Reactions, edited markers, reply counts and read state are deliberately out.
        hash_fields={"files": sorted(f["name"] for f in files)},
    )


def directory_from_users(users: list[dict[str, Any]]) -> Directory:
    """users.list members -> Directory."""
    return Directory(
        users={u["id"]: handle(u) for u in users if u.get("id")},
        bots=frozenset(u["id"] for u in users if u.get("is_bot") or u.get("id") == SLACKBOT),
    )


def handle(user: dict[str, Any]) -> str:
    """Slack's unique username; falls back to display / real name."""
    profile = user.get("profile") or {}
    return (
        user.get("name")
        or profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user["id"]
    )


def is_full_member(user: dict[str, Any]) -> bool:
    """Active, human, non-guest member: can read every public channel in Slack."""
    return not (
        user.get("deleted")
        or user.get("is_bot")
        or user.get("id") == SLACKBOT
        or user.get("is_restricted")
        or user.get("is_ultra_restricted")
    )


def is_active_human(user: dict[str, Any]) -> bool:
    return not (user.get("deleted") or user.get("is_bot") or user.get("id") == SLACKBOT)
