"""NotionSource against the fake Notion API: resolve, subtree filtering, cursors, access."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tacit.config import Config, SourcesConfig
from tacit.sources.base import ScopeEntry, SourceAccessDenied
from tacit.sources.notion.source import NotionSource
from tacit.store import Store
from tacit.store import state as st
from tacit.sync import run_sync

from .notion_mock import API, block, data_source, database, notion_api, page, parent_block

__all__ = ["notion_api"]  # the fixture

TOKEN = "ntn_test_token"
LONG = "We moved the job queue from Redis to Postgres because one database is simpler. " * 4


def _id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


ROOT, CHILD, GRAND, INCOL, ROW = (_id(x) for x in ("root", "child", "grand", "incol", "row"))
OTHER, OTHER_CHILD, TRASHED, HIDDEN, BEHIND = (
    _id(x) for x in ("other", "other-child", "trashed", "hidden", "behind")
)
COLUMN, DB, DS = _id("column"), _id("db"), _id("ds")


def ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:00.000Z")


def config(**sources) -> Config:
    return Config(sources=SourcesConfig(notion_api_url=API, **sources))


@pytest.fixture
def workspace(notion_api):
    """ROOT ─┬─ CHILD ─┬─ GRAND
             │         └─ database DB / data source DS ─ ROW
             ├─ column block ─ INCOL
             └─ TRASHED (in trash)
    OTHER ─ OTHER_CHILD   (shared, outside the scope)
    HIDDEN ─ BEHIND       (HIDDEN isn't shared, so BEHIND can't be placed)"""
    f = notion_api
    f.add(
        page(ROOT, "Eng Handbook", edited=ago(days=3)),
        page(CHILD, "Decisions", parent=("page", ROOT), edited=ago(days=2)),
        page(GRAND, "Queue decision", parent=("page", CHILD), edited=ago(hours=5)),
        parent_block(COLUMN, ("page", ROOT)),
        page(INCOL, "Column page", parent=("block", COLUMN), edited=ago(days=1)),
        database(DB, "ADRs", ("page", CHILD)),
        data_source(DS, "ADRs", DB, ("page", CHILD)),
        page(ROW, "ADR 7", edited=ago(hours=3), author="nu-bob", editor="nu-alice"),
        page(OTHER, "Marketing", edited=ago(hours=1)),
        page(OTHER_CHILD, "Launch plan", parent=("page", OTHER), edited=ago(hours=2)),
        page(TRASHED, "Old idea", parent=("page", ROOT), edited=ago(hours=4), in_trash=True),
        page(HIDDEN, "Secret", edited=ago(days=4)),
        page(BEHIND, "Behind secret", parent=("page", HIDDEN), edited=ago(days=4)),
    )
    # A database row's parent (Notion-Version 2025-09-03) names the data source + database.
    f.objects[ROW]["parent"] = {"type": "data_source_id", "data_source_id": DS, "database_id": DB}
    f.objects[ROW]["properties"]["Status"] = {"type": "status", "status": {"name": "Decided"}}
    f.children[GRAND] = [
        block("heading_1", "Decision"),
        block("toggle", "Why", bid="tog") | {"has_children": True},
        block("child_page", title="Appendix"),
    ]
    f.children["tog"] = [block("paragraph", LONG)]
    f.children[ROOT] = [block("paragraph", "Start here.")]
    f.users = [{"object": "user", "id": "nu-alice", "name": "Alice"}]
    f.access[TOKEN] = set(f.objects) - {HIDDEN}
    f.hidden_from_search.add(COLUMN)
    return f


async def collect(source: NotionSource, entry: ScopeEntry, cursor: str | None = None):
    return [p async for p in source.fetch(entry, cursor)]


async def entry_for(source: NotionSource) -> ScopeEntry:
    return await source.resolve(ROOT)


# --- resolve --------------------------------------------------------------------------


async def test_resolve_accepts_ids_and_urls(workspace):
    src = NotionSource(TOKEN, config())
    compact = ROOT.replace("-", "")
    for target in (ROOT, compact, f"https://www.notion.so/acme/Eng-Handbook-{compact}?v=1"):
        entry = await src.resolve(target)
        assert entry == ScopeEntry(f"notion:{ROOT}", "notion", ROOT, "Eng Handbook", {})


async def test_resolve_explains_unshared_and_bad_targets(workspace):
    src = NotionSource(TOKEN, config())
    with pytest.raises(ValueError, match="share the page with your Tacit integration"):
        await src.resolve(HIDDEN)
    with pytest.raises(ValueError, match="isn't a Notion page id or URL"):
        await src.resolve("eng-handbook")
    with pytest.raises(ValueError, match="trash"):
        await src.resolve(TRASHED)


# --- fetch ----------------------------------------------------------------------------


async def test_fetch_keeps_only_the_subtree_and_renders_pages(workspace):
    src = NotionSource(TOKEN, config())
    entry = await entry_for(src)
    pages = await collect(src, entry)
    items = {i.external_id: i for p in pages for i in p.items}
    assert set(items) == {f"notion:{x}" for x in (ROOT, CHILD, GRAND, INCOL, ROW)}
    grand = items[f"notion:{GRAND}"]
    assert grand.text.startswith("# Decision\nWhy\n  We moved the job queue")
    assert "[page: Appendix]" in grand.text
    assert grand.extract is True and items[f"notion:{CHILD}"].extract is False
    assert grand.url.endswith(GRAND.replace("-", "")) and grand.scope_key == entry.key
    row = items[f"notion:{ROW}"]
    assert row.text.startswith("Status: Decided")
    assert row.participants == ("notion:nu-bob", "notion:Alice")
    # Each ancestor is looked up once per run, however many pages sit below it.
    assert len(workspace.calls_to("databases/")) == 1
    assert len(workspace.calls_to(f"pages/{HIDDEN}")) == 1


async def test_cursor_only_moves_on_the_last_batch(workspace):
    workspace.search_page_size = 3
    src = NotionSource(TOKEN, config())
    pages = await collect(src, await entry_for(src))
    assert len(pages) > 1
    assert all(p.cursor is None for p in pages[:-1])
    newest = max(
        o["last_edited_time"] for o in workspace.objects.values() if o.get("object") == "page"
    )
    assert json.loads(pages[-1].cursor)["last_edited"][:16] == newest[:16]


async def test_incremental_fetch_stops_at_the_cursor(workspace):
    workspace.search_page_size = 2
    src = NotionSource(TOKEN, config())
    entry = await entry_for(src)
    first = await collect(src, entry)
    cursor = first[-1].cursor
    workspace.calls.clear()

    # Nothing edited since: only the overlap window is re-read.
    again = await collect(src, entry, cursor)
    assert [i for p in again for i in p.items] == []
    assert len(workspace.calls_to("search")) == 1

    workspace.objects[CHILD]["last_edited_time"] = ago(minutes=0)
    changed = await collect(src, entry, cursor)
    assert [i.external_id for p in changed for i in p.items] == [f"notion:{CHILD}"]
    assert json.loads(changed[-1].cursor)["last_edited"] >= json.loads(cursor)["last_edited"]


async def test_first_sync_looks_back_backfill_days(workspace):
    src = NotionSource(TOKEN, config(backfill_days=2))
    pages = await collect(src, await entry_for(src))
    got = {i.external_id for p in pages for i in p.items}
    assert got == {f"notion:{x}" for x in (GRAND, INCOL, ROW)}  # ROOT/CHILD are older


async def test_fetch_raises_access_denied_when_the_root_is_unshared(workspace):
    src = NotionSource(TOKEN, config())
    entry = await entry_for(src)
    workspace.access[TOKEN].discard(ROOT)
    with pytest.raises(SourceAccessDenied, match="no longer shared"):
        await collect(src, entry)


async def test_rate_limited_search_is_retried(workspace, monkeypatch):
    import tacit.sources.notion.client as nc

    async def no_sleep(_):
        return None

    monkeypatch.setattr(nc.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(nc, "_client", _no_sdk_retries(nc._client))
    real = workspace.handle
    hits = {"n": 0}

    def flaky(request):
        if request.url.path.endswith("/search") and hits["n"] == 0:
            hits["n"] += 1
            return workspace._error(429, "rate_limited", {"retry-after": "1"})
        return real(request)

    workspace.handle = flaky
    src = NotionSource(TOKEN, config())
    pages = await collect(src, await entry_for(src))
    assert hits["n"] == 1 and sum(len(p.items) for p in pages) == 5


def _no_sdk_retries(make):
    def wrapped(token, base_url):
        client = make(token, base_url)
        client._max_retries = 0
        return client

    return wrapped


# --- end to end -----------------------------------------------------------------------


async def test_run_sync_with_notion_source(workspace, store: Store):
    src = NotionSource(TOKEN, config())
    await st.scope_add(store, await entry_for(src))
    r1 = await run_sync(store, {"notion": src})
    assert r1.scopes[0].error is None and r1.scopes[0].written == 5
    assert r1.extraction_pending == 1  # only the long page is worth extracting
    r2 = await run_sync(store, {"notion": src})
    assert r2.scopes[0].written == 0
    doc = await store._execute(
        "MATCH (d:TacitDoc {external_id: $id}) RETURN d.url AS url", {"id": f"notion:{GRAND}"}
    )
    assert doc[0]["url"] == workspace.objects[GRAND]["url"]
