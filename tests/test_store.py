import asyncio
from datetime import UTC, datetime

import pytest

from tacit.sources.base import Item, ScopeEntry
from tacit.store import AccessLogError, Store
from tacit.store import docs as d
from tacit.store import state as st
from tacit.store.schema import SchemaMismatch, ensure_schema


def _item(eid="github:1", text="We picked Redis for pub/sub.") -> Item:
    return Item(
        source="github",
        scope_key="github:acme/api",
        external_id=eid,
        kind="pull_request",
        url=f"https://github.com/acme/api/pull/{eid[-1]}",
        title="Use Redis",
        text=text,
        reference_time=datetime(2026, 3, 2, tzinfo=UTC),
        participants=("github:dana",),
        metadata={"number": 1},
    )


async def test_every_query_is_logged_first(store: Store):
    await store.run_query("read", "search", "RETURN 1 AS x", {"query": "why redis"})
    log = await st.access_log(store, grep="why redis")
    assert log and log[0]["tool"] == "search" and log[0]["who"] == "tester@example.com"
    assert log[0]["result_count"] == 1


async def test_query_does_not_run_if_log_fails(store: Store, monkeypatch):
    calls = []
    real = store._execute

    async def broken(cypher, params=None):
        if "TacitAccess" in cypher:
            raise RuntimeError("disk full")
        calls.append(cypher)
        return await real(cypher, params)

    monkeypatch.setattr(store, "_execute", broken)
    with pytest.raises(AccessLogError):
        await store.run_query("write", "doc.upsert", "CREATE (:Boom)")
    assert calls == []


async def test_access_context_records_outcome(store: Store):
    async with store.access("write", "extract", {"external_id": "x"}) as out:
        out["cost_usd"] = 0.01
    log = await st.access_log(store, grep="extract")
    assert log[0]["cost_usd"] == 0.01


async def test_doc_upsert_replaces_chunks_and_hash(store: Store):
    item = _item()
    await d.upsert_doc(store, item, ["a", "b"], [[0.1] * 384, [0.2] * 384])
    assert await d.known_hashes(store, ["github:1"]) == {"github:1": item.content_hash}
    changed = _item(text="We moved to Valkey.")
    await d.upsert_doc(store, changed, ["c"], [[0.3] * 384])
    rows = await store._execute("MATCH (:TacitDoc)-[:HAS_CHUNK]->(c) RETURN c.text AS t")
    assert [r["t"] for r in rows] == ["c"]
    doc = await d.get_doc(store, item.url)
    assert doc and doc["title"] == "Use Redis" and doc["metadata"] == {"number": 1}
    assert await d.delete_doc(store, "github:1") == ""
    assert await d.get_doc(store, "github:1") is None


async def test_scope_and_cursors(store: Store):
    e = ScopeEntry("slack:C1", "slack", "C1", "#eng", {"private": False})
    await st.scope_add(store, e)
    assert await st.scope_list(store) == [e]
    await st.cursor_set(store, "slack:C1", "1700000000.0001")
    assert await st.cursor_get(store, "slack:C1") == "1700000000.0001"
    assert await st.scope_remove(store, "slack:C1")
    assert await st.scope_list(store) == []


async def test_lease_is_exclusive_and_expires(store: Store):
    a, b = st.Lease(store, ttl_seconds=60), st.Lease(store, ttl_seconds=60)
    results = await asyncio.gather(a.claim(), b.claim())
    assert sorted(r[0] for r in results) == [False, True]
    winner, loser = (a, b) if results[0][0] else (b, a)
    assert await winner.renew()
    assert not await loser.renew()
    await winner.release()
    assert (await loser.claim())[0]
    # An expired lease can be taken over.
    await store._execute(
        "MATCH (l:TacitLease) SET l.expires_at = datetime() - duration({seconds: 1})"
    )
    assert (await winner.claim())[0]


async def test_schema_refuses_other_embedder(store: Store):
    with pytest.raises(SchemaMismatch):
        await ensure_schema(store, "BAAI/bge-small-en-v1.5")


async def _expire_lease(store: Store) -> None:
    await store._execute(
        "MATCH (l:TacitLease) SET l.expires_at = datetime() - duration({seconds: 1})"
    )


async def test_lease_token_increments_only_for_a_new_holder(store: Store):
    a, b = st.Lease(store, ttl_seconds=60), st.Lease(store, ttl_seconds=60)
    assert (await a.claim())[0]
    first = a.token
    assert isinstance(first, int)
    assert (await a.claim())[0] and a.token == first  # re-claim by the same holder
    assert not (await b.claim())[0] and b.token is None
    await _expire_lease(store)
    assert (await b.claim())[0] and b.token == first + 1  # takeover after expiry
    await b.release()
    assert (await a.claim())[0] and a.token == first + 2  # free lease, new holder


async def test_fenced_writes_with_a_stale_token_write_nothing(store: Store):
    stale, fresh = st.Lease(store, ttl_seconds=60), st.Lease(store, ttl_seconds=60)
    assert (await stale.claim())[0]
    item = _item()
    await d.upsert_doc(store, item, ["a", "b"], [[0.1] * 384, [0.2] * 384], fence=stale)
    await st.cursor_set(store, "github:acme/api", "c1", fence=stale)
    await d.set_episode(store, item.external_id, "ep-1", item.content_hash, fence=stale)

    await _expire_lease(store)  # `stale` froze past its TTL; someone else takes over
    assert (await fresh.claim())[0]

    changed = _item(text="We picked Kafka instead.")
    with pytest.raises(st.FencedOut):
        await d.upsert_doc(store, changed, ["x"], [[0.3] * 384], fence=stale)
    with pytest.raises(st.FencedOut):
        await st.cursor_set(store, "github:acme/api", "c2", fence=stale)
    with pytest.raises(st.FencedOut):
        await d.set_episode(store, item.external_id, "ep-2", "h2", fence=stale)
    assert stale.lost

    doc = await d.get_doc(store, item.external_id)
    assert doc["text"] == item.text
    chunks = await store._execute("MATCH (c:TacitChunk) RETURN c.text AS t ORDER BY c.idx")
    assert [c["t"] for c in chunks] == ["a", "b"]
    assert await st.cursor_get(store, "github:acme/api") == "c1"
    assert await d.episode_of(store, item.external_id) == "ep-1"
    lease = await store._execute("MATCH (l:TacitLease) RETURN l._fence AS f, l.token AS t")
    assert lease[0]["f"] is None and lease[0]["t"] == fresh.token

    # The current holder's token still writes; unfenced callers are unaffected.
    await st.cursor_set(store, "github:acme/api", "c3", fence=fresh)
    assert await st.cursor_get(store, "github:acme/api") == "c3"
    await st.cursor_set(store, "github:acme/api", "c4")
    assert await st.cursor_get(store, "github:acme/api") == "c4"
