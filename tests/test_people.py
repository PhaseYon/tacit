from dataclasses import replace

from tacit.store import Store
from tacit.store import people as p
from tacit.sync import index_item

from .test_sync import ITEMS


async def _seed(store: Store):
    gh = replace(ITEMS[0], participants=("github:dana-gh", "github:lee"))
    sl = replace(
        ITEMS[1],
        external_id="slack:C1:1.0",
        source="slack",
        kind="slack_thread",
        url="https://acme.slack.com/archives/C1/p1",
        participants=("slack:U1",),
        metadata={"handles": {"slack:U1": "dana"}, "emails": {"slack:U1": "dana@acme.com"}},
    )
    nt = replace(
        ITEMS[1],
        external_id="notion:abc",
        source="notion",
        kind="notion_page",
        url="https://notion.so/abc",
        participants=("notion:Dana Lee",),
        metadata={"emails": {"notion:Dana Lee": "Dana@acme.com"}},
    )
    for item in (gh, sl, nt):
        await index_item(store, item)


async def test_link_moves_handles_and_builds_header(store: Store):
    await _seed(store)
    await p.link(store, "Dana Lee", ["github:dana-gh", "slack:U1"])
    header = await p.identity_header(store, ["slack:U1"], {"handles": {"slack:U1": "dana"}})
    assert "Dana Lee: @dana-gh on GitHub, @dana on Slack" in header
    # Linking a handle to someone else moves it; a person left with no handles disappears.
    await p.link(store, "Someone Else", ["github:dana-gh"])
    people = {x["name"]: x["handles"] for x in await p.people(store)}
    assert people == {"Dana Lee": ["slack:U1"], "Someone Else": ["github:dana-gh"]}
    assert await p.identity_header(store, ["slack:U1"], {}) == ""  # one handle: nothing to say
    assert await p.unlink(store, "slack:U1")
    assert {x["name"] for x in await p.people(store)} == {"Someone Else"}


async def test_unlinked_and_auto_link_by_email(store: Store):
    await _seed(store)
    seen = {u["handle"]: u for u in await p.unlinked(store)}
    assert seen["slack:U1"]["display"] == "dana" and seen["slack:U1"]["email"] == "dana@acme.com"
    done = await p.auto_link(store)
    assert done == [("dana", ["notion:Dana Lee", "slack:U1"])]
    assert "slack:U1" not in {u["handle"] for u in await p.unlinked(store)}
    assert await p.auto_link(store) == []  # idempotent


async def test_extraction_gets_identity_header(store: Store):
    from tacit.graph.client import Extractor

    from .test_sync import FakeProvider

    await _seed(store)
    await p.link(store, "Dana Lee", ["github:dana-gh", "slack:U1"])
    provider = FakeProvider()
    seen_bodies: list[str] = []
    original = provider.complete_structured

    async def spy(messages, schema, **kw):
        seen_bodies.extend(m.content for m in messages)
        return await original(messages, schema, **kw)

    provider.complete_structured = spy
    ex = Extractor(store, provider, max_usd=100)
    await ex.setup()
    try:
        from tacit.store import docs as d

        item = next(i for i in await d.pending_extraction(store) if i.source == "github")
        await ex.extract(item)
    finally:
        await ex.close()
    assert any("Dana Lee: @dana-gh on GitHub" in b for b in seen_bodies)
