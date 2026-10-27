from datetime import UTC, datetime
from decimal import Decimal

from tacit.llm.base import Usage
from tacit.sources.base import Item, Page, ScopeEntry, SourceAccessDenied
from tacit.store import Store
from tacit.store import docs as d
from tacit.store import state as st
from tacit.sync import run_sync

from .graph_fake_llm import respond


class FakeSource:
    name = "github"

    def __init__(self, items: list[Item], deny: set[str] = frozenset()) -> None:
        self.items = items
        self.deny = deny
        self.cursors_seen: list[str | None] = []

    async def resolve(self, target: str) -> ScopeEntry:
        return ScopeEntry(f"github:{target}", "github", target, target)

    async def fetch(self, entry: ScopeEntry, cursor: str | None):
        self.cursors_seen.append(cursor)
        if entry.key in self.deny:
            raise SourceAccessDenied("no access")
        yield Page(self.items[:1], "c1")
        yield Page(self.items[1:], "c2")


class FakeProvider:
    name = "fake"

    def __init__(self, cost_per_call: str = "0.001") -> None:
        self.cost = Decimal(cost_per_call)
        self.calls = 0

    def model_for(self, tier):
        return "fake-model"

    async def complete_structured(self, messages, schema, *, tier="default", max_tokens=8192):
        self.calls += 1
        return respond(messages, schema), Usage("fake", "fake-model", 10, 0, 5, self.cost)


def _item(n: int, text: str) -> Item:
    return Item(
        source="github",
        scope_key="github:acme/api",
        external_id=f"github:PR_{n}",
        kind="pull_request",
        url=f"https://github.com/acme/api/pull/{n}",
        title=f"PR {n}",
        text=text,
        reference_time=datetime(2026, 3, n, tzinfo=UTC),
        participants=("github:dana",),
    )


ITEMS = [
    _item(1, "@dana: we switch from Postgres to Redis for the api cache."),
    _item(2, "@lee: adopt Kafka for billing events."),
]


async def test_sync_indexes_skips_unchanged_and_saves_cursor(store: Store):
    await st.scope_add(store, ScopeEntry("github:acme/api", "github", "acme/api", "acme/api"))
    src = FakeSource(ITEMS)
    r1 = await run_sync(store, {"github": src})
    assert r1.scopes[0].written == 2 and r1.scopes[0].unchanged == 0
    assert await st.cursor_get(store, "github:acme/api") == "c2"
    r2 = await run_sync(store, {"github": src})
    assert r2.scopes[0].written == 0 and r2.scopes[0].unchanged == 2
    assert src.cursors_seen == [None, "c2"]
    chunks = await store._execute("MATCH (c:TacitChunk) RETURN count(c) AS n")
    assert chunks[0]["n"] == 2
    assert r2.extraction_pending == 2  # indexed, not yet extracted


async def test_sync_skips_sources_without_token_or_access(store: Store):
    await st.scope_add(store, ScopeEntry("github:acme/api", "github", "acme/api", "acme/api"))
    await st.scope_add(store, ScopeEntry("slack:C1", "slack", "C1", "#eng"))
    r = await run_sync(store, {"github": FakeSource(ITEMS, deny={"github:acme/api"})})
    reasons = {s.key: s.skipped_reason for s in r.scopes}
    assert reasons == {"github:acme/api": "no access", "slack:C1": "no slack token"}


async def test_sync_exits_when_someone_else_holds_the_lease(store: Store):
    other = st.Lease(store)
    assert (await other.claim())[0]
    r = await run_sync(store, {"github": FakeSource(ITEMS)})
    assert r.blocked_by == "tester@example.com" and r.scopes == []
    await other.release()


async def test_extraction_creates_episodes_and_respects_budget(store: Store):
    from tacit.graph.client import Extractor

    await st.scope_add(store, ScopeEntry("github:acme/api", "github", "acme/api", "acme/api"))
    provider = FakeProvider()
    ex = Extractor(store, provider, max_usd=100)
    await ex.setup()
    try:
        r = await run_sync(store, {"github": FakeSource(ITEMS)}, extractor=ex)
    finally:
        await ex.close()
    assert r.extracted == 2 and r.extraction_pending == 0 and r.spent_usd > 0
    ep = await d.episode_of(store, "github:PR_1")
    assert ep
    cites = await d.docs_for_episodes(store, [ep])
    assert cites[ep]["url"].endswith("/pull/1")
    log = await st.access_log(store, grep="extract")
    assert any(e["cost_usd"] for e in log)

    # A tiny budget stops extraction before the second item.
    changed = [_item(1, ITEMS[0].text + " Also adopt Kafka."), _item(2, ITEMS[1].text + "!")]
    ex2 = Extractor(store, FakeProvider("1"), max_usd=0.5)
    try:
        r2 = await run_sync(store, {"github": FakeSource(changed)}, extractor=ex2)
    finally:
        await ex2.close()
    assert r2.extraction_note and "budget" in r2.extraction_note
    assert r2.extraction_pending >= 1


async def test_items_marked_extract_false_are_indexed_but_not_extracted(store: Store):
    from dataclasses import replace

    await st.scope_add(store, ScopeEntry("github:acme/api", "github", "acme/api", "acme/api"))
    items = [ITEMS[0], replace(ITEMS[1], extract=False)]
    r = await run_sync(store, {"github": FakeSource(items)})
    assert r.scopes[0].written == 2 and r.extraction_pending == 1


def test_canonical_prompt_sorts_entity_type_lists():
    from tacit.graph.adapters import canonical_prompt

    a = '[{"name": "ES", "entity_types": ["Technology", "Entity"]}]'
    b = '[{"name": "ES", "entity_types": ["Entity", "Technology"]}]'
    assert canonical_prompt(a) == canonical_prompt(b) == b
    c = "{'name': 'api', 'entity_types': ['Entity', 'Component'], 'attributes': {}}"
    assert (
        canonical_prompt(c)
        == "{'name': 'api', 'entity_types': ['Component', 'Entity'], 'attributes': {}}"
    )
    assert canonical_prompt("no lists here") == "no lists here"
