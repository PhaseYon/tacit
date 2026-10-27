"""SlackSource: a channel -> Items, using the syncing user's own user token (xoxp-).

One Item per thread (parent + every reply) and one Item per channel per UTC day for
top-level messages that have no replies. Transcripts read `@handle (YYYY-MM-DD HH:MM):
text`, times in UTC.

Incremental sync: the cursor is `{"latest_ts": "<newest top-level ts seen>"}`. Each run
re-reads from LOOKBACK before it (rounded down to UTC midnight, so a day Item is always
rebuilt from the whole day), which catches new replies on recent threads and edits. The
orchestrator's content-hash skip makes those re-reads free. Replies to threads whose
parent is older than the lookback window are not picked up.
"""

import json
import logging
import re
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from ...config import Config
from ..base import Item, Page, ScopeEntry, SourceAccessDenied
from . import normalize as n
from .client import SlackError, error_code, get_all, get_page, make_client

log = logging.getLogger(__name__)

LOOKBACK = timedelta(days=7)
PAGE_ITEMS = 25
THREAD_EXTRACT_MIN = 2  # human messages before a thread is worth LLM extraction
DAY_EXTRACT_MIN = 3
CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{6,}$")
# Errors meaning "this token can't read this channel": skip it, a teammate may can.
DENIED = {
    "not_in_channel",
    "channel_not_found",
    "missing_scope",
    "access_denied",
    "no_permission",
    "not_allowed_token_type",
}
NEVER_SYNCABLE = "DMs and group DMs are never syncable; add a channel instead."


def _day_start(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _ts(dt: datetime) -> str:
    return f"{dt.timestamp():.6f}"


class SlackSource:
    name = "slack"

    def __init__(self, token: str, config: Config) -> None:
        self.config = config
        self.client = make_client(token, config.sources.slack_api_url)
        self._directory: n.Directory | None = None
        self._domain_loaded = False
        self._team_domain: str | None = None

    # ---- resolve -------------------------------------------------------------------

    async def resolve(self, target: str) -> ScopeEntry:
        t = target.strip()
        if CHANNEL_ID.match(t):
            if t.startswith("D"):
                raise ValueError(NEVER_SYNCABLE)
            ch = await self._info(t)
        else:
            ch = await self._find_by_name(t.removeprefix("#").lower())
        if ch.get("is_im") or ch.get("is_mpim"):
            raise ValueError(NEVER_SYNCABLE)
        private = bool(ch.get("is_private") or ch.get("is_group"))
        return ScopeEntry(
            key=f"slack:{ch['id']}",
            source="slack",
            target=ch["id"],
            label=f"#{ch.get('name') or ch['id']}",
            options={"private": private},
        )

    async def _info(self, channel_id: str) -> dict[str, Any]:
        try:
            resp = await self.client.conversations_info(channel=channel_id)
        except SlackError as exc:
            raise ValueError(
                f"Can't read Slack channel {channel_id} with your token ({error_code(exc)})."
            ) from exc
        return resp["channel"]

    async def _find_by_name(self, name: str) -> dict[str, Any]:
        cursor: str | None = None
        while True:
            try:
                page, cursor, _ = await get_page(
                    self.client.conversations_list,
                    "channels",
                    cursor,
                    types="public_channel,private_channel",
                    exclude_archived=True,
                )
            except SlackError as exc:
                raise ValueError(f"Can't list Slack channels ({error_code(exc)}).") from exc
            for ch in page:
                if name in (ch.get("name"), ch.get("name_normalized")):
                    return ch
            if not cursor:
                raise ValueError(
                    f"No Slack channel #{name} visible to your token. Private channels"
                    " are only visible to their members."
                )

    # ---- fetch ---------------------------------------------------------------------

    async def fetch(self, entry: ScopeEntry, cursor: str | None) -> AsyncIterator[Page]:
        latest = (json.loads(cursor) if cursor else {}).get("latest_ts")
        if latest:
            start = n.parse_ts(latest) - LOOKBACK
        else:
            start = datetime.now(UTC) - timedelta(days=self.config.sources.backfill_days)
        oldest = _day_start(start)

        history = await self._call(
            entry,
            get_all(
                self.client.conversations_history,
                "messages",
                channel=entry.target,
                oldest=_ts(oldest),
            ),
        )
        directory = await self.directory()
        channel = n.Channel("", entry.target, entry.label, await self.team_domain())

        threads = [m for m in history if m.get("reply_count") and m.get("thread_ts") == m["ts"]]
        thread_ids = {m["ts"] for m in threads}
        days: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for m in history:
            if m["ts"] in thread_ids:
                continue
            if m.get("thread_ts") and m["thread_ts"] in thread_ids:
                continue  # a reply broadcast to the channel: already in its thread
            if n.is_human_message(m, directory):
                days[n.parse_ts(m["ts"]).date().isoformat()].append(m)

        # Units in order of their newest top-level message, so a page's cursor (the newest
        # top-level ts written so far) never skips past an unwritten unit by > LOOKBACK.
        units: list[tuple[str, str, Any]] = [(m["ts"], "thread", m) for m in threads]
        units += [(max((m["ts"] for m in ms), key=float), "day", d) for d, ms in days.items()]
        units.sort(key=lambda u: float(u[0]))
        newest = max((m["ts"] for m in history), key=float, default=latest)

        if not units:
            if newest:
                yield Page([], json.dumps({"latest_ts": newest}))
            return
        for i in range(0, len(units), PAGE_ITEMS):
            batch = units[i : i + PAGE_ITEMS]
            items: list[Item] = []
            for _, kind, payload in batch:
                if kind == "thread":
                    item = await self._thread_item(entry, channel, payload, directory)
                else:
                    item = self._day_item(entry, channel, payload, days[payload], directory)
                if item is not None:
                    items.append(item)
            last = i + PAGE_ITEMS >= len(units)
            page_ts = newest if last else batch[-1][0]
            yield Page(items, json.dumps({"latest_ts": page_ts}))

    async def _call(self, entry: ScopeEntry, coro):
        try:
            return await coro
        except SlackError as exc:
            code = error_code(exc)
            if code in DENIED:
                raise SourceAccessDenied(f"can't read {entry.label}: {code}") from exc
            raise

    async def _thread_item(
        self, entry: ScopeEntry, ch: n.Channel, parent: dict[str, Any], directory: n.Directory
    ) -> Item | None:
        try:
            msgs = await self._call(
                entry,
                get_all(
                    self.client.conversations_replies,
                    "messages",
                    channel=entry.target,
                    ts=parent["ts"],
                ),
            )
        except SlackError as exc:
            if error_code(exc) == "thread_not_found":  # parent deleted mid-sync
                return None
            raise
        msgs = sorted({m["ts"]: m for m in [parent, *msgs]}.values(), key=lambda m: float(m["ts"]))
        return self._item(
            entry,
            kind="slack_thread",
            external_id=f"slack:{entry.target}:{parent['ts']}",
            url=n.permalink(ch, parent["ts"]),
            title_from=parent,
            fallback_title=f"Thread in {entry.label}",
            msgs=msgs,
            directory=directory,
            extract_min=THREAD_EXTRACT_MIN,
            extra={"thread_ts": parent["ts"]},
        )

    def _day_item(
        self,
        entry: ScopeEntry,
        ch: n.Channel,
        day: str,
        msgs: list[dict[str, Any]],
        directory: n.Directory,
    ) -> Item | None:
        msgs = sorted(msgs, key=lambda m: float(m["ts"]))
        return self._item(
            entry,
            kind="slack_day",
            external_id=f"slack:{entry.target}:day:{day}",
            url=n.permalink(ch, msgs[0]["ts"]),
            title_from=None,
            fallback_title=f"{entry.label} on {day}",
            msgs=msgs,
            directory=directory,
            extract_min=DAY_EXTRACT_MIN,
            extra={"date": day},
        )

    def _item(
        self,
        entry: ScopeEntry,
        *,
        kind: str,
        external_id: str,
        url: str,
        title_from: dict[str, Any] | None,
        fallback_title: str,
        msgs: list[dict[str, Any]],
        directory: n.Directory,
        extract_min: int,
        extra: dict[str, Any],
    ) -> Item | None:
        lines: list[str] = []
        handles: dict[str, str] = {}
        emails: dict[str, str] = {}
        times: list[datetime] = []
        title = ""
        for m in msgs:
            if not n.is_human_message(m, directory):
                continue
            text, _ = n.message_text(m, directory)
            if not text:
                continue
            uid = m["user"]
            handle = directory.users.get(uid) or (m.get("user_profile") or {}).get("name") or uid
            when = n.parse_ts(m["ts"])
            lines.append(f"@{handle} ({when:%Y-%m-%d %H:%M}): {text}")
            handles[f"slack:{uid}"] = handle
            if email := directory.emails.get(uid):
                emails[f"slack:{uid}"] = email
            times.append(when)
            # Thread title: the parent's first line (or the first human message's, if a
            # bot started the thread). Day title: fixed.
            if title_from is not None and (m["ts"] == title_from["ts"] or not title):
                title = n.first_line(text)
        if not lines:
            return None
        metadata: dict[str, Any] = {
            "channel": entry.label,
            "channel_id": entry.target,
            "handles": handles,
            "message_count": len(lines),
            **extra,
        }
        if emails:
            metadata["emails"] = emails
        return Item(
            source="slack",
            scope_key=entry.key,
            external_id=external_id,
            kind=kind,
            url=url,
            title=title or fallback_title,
            text="\n".join(lines),
            reference_time=max(times),
            participants=tuple(sorted(handles)),
            metadata=metadata,
            extract=len(lines) >= extract_min,
        )

    # ---- per-run caches ------------------------------------------------------------

    async def directory(self) -> n.Directory:
        """users.list once per run: handles for transcripts, bot ids, optional emails."""
        if self._directory is None:
            try:
                users = await get_all(self.client.users_list, "members")
            except SlackError as exc:
                log.warning("users.list failed (%s); showing raw user ids", error_code(exc))
                users = []
            self._directory = n.directory_from_users(users)
        return self._directory

    async def team_domain(self) -> str | None:
        """`acme` from auth.test's https://acme.slack.com/, for permalinks."""
        if not self._domain_loaded:
            self._domain_loaded = True
            try:
                host = urlparse((await self.client.auth_test()).get("url") or "").hostname
                if host and host.endswith(".slack.com"):
                    self._team_domain = host.removesuffix(".slack.com")
            except SlackError as exc:
                log.warning("auth.test failed (%s); using generic permalinks", error_code(exc))
        return self._team_domain
