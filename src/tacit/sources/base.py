"""The contract every source (GitHub, Slack, Notion) implements.

A source turns one scope entry (e.g. `github:acme/api`) into Items, page by page, using
the syncing user's own token. An Item is one "unit of meaning" that becomes one TacitDoc
(chunked and embedded for search) and, when extraction is on, one Graphiti episode:

- GitHub: a merged PR / closed+discussed issue with its comments and reviews, or a doc file
- Slack: a thread, or a day of top-level messages in a channel
- Notion: a page

Sources never touch Neo4j. The sync orchestrator (tacit.sync) skips Items whose
content_hash is unchanged, writes the rest under the write lease, and only then saves
the page's cursor, so a crash mid-page just redoes that page.
"""

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class ScopeEntry:
    """One row of :TacitScope. key = "<source>:<target>", e.g. "slack:C0123ABC"."""

    key: str
    source: str  # github | slack | notion
    target: str  # "acme/api" | channel id | Notion page id
    label: str  # human name shown in `tacit scope ls` ("#eng", page title)
    options: dict[str, Any] = field(default_factory=dict)  # e.g. {"private": true}


@dataclass(frozen=True)
class Item:
    source: str
    scope_key: str  # the ScopeEntry.key this came from
    external_id: str  # globally unique and stable, prefixed by source: "github:PR_kwDO..."
    kind: str  # pull_request | issue | markdown_file | slack_thread | slack_day | notion_page
    url: str  # exact citation link
    title: str
    text: str  # rendered transcript: what gets chunked, embedded and extracted
    reference_time: datetime | None  # when this happened (merge time, last message)
    # People involved, as "<source>:<handle>" ("github:octocat", "slack:U0123").
    participants: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    # Worth paying for LLM extraction? Everything is indexed for search (free); only
    # items with extract=True become Graphiti episodes (e.g. merged PRs, not open drafts).
    extract: bool = True

    @property
    def content_hash(self) -> str:
        """What counts as "changed". Only kind/title/text: put anything else that should
        trigger re-indexing (e.g. "State: merged") into the rendered text."""
        payload = {"kind": self.kind, "title": self.title, "text": self.text}
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True)
class Page:
    items: list[Item]
    # Opaque cursor to persist once every item in this page is written. None = keep old.
    cursor: str | None


class Source(Protocol):
    name: str  # github | slack | notion

    async def resolve(self, target: str) -> ScopeEntry:
        """Validate a `tacit scope add` target with the user's token and return the entry.
        Raise ValueError with a readable message if it doesn't exist or isn't allowed."""
        ...

    def fetch(self, entry: ScopeEntry, cursor: str | None) -> AsyncIterator[Page]:
        """Yield pages of items changed since `cursor` (None = first sync, bounded by
        config.sources.backfill_days). Raise SourceAccessDenied if this user's token
        can't read the entry, so the orchestrator skips it and someone else syncs it."""
        ...


class SourceAccessDenied(RuntimeError):
    pass


class SourceNotConfigured(RuntimeError):
    pass
