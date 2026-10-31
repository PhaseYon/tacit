"""Stress tests for the write lease, fencing tokens and idempotent writes.

Each "machine" is its own Store (its own driver and connection pool), like separate
laptops sharing one team graph. Fake sources and a fake LLM: no network, $0.
Run with `-s` to see the numbers.
"""

import asyncio
import random
import time
from datetime import UTC, datetime

from tacit.config import get_config
from tacit.sources.base import Item, Page, ScopeEntry
from tacit.store import Store
from tacit.store import docs as d
from tacit.store import state as st
from tacit.sync import FencedOut, LeaseLost, run_sync

from .test_sync import FakeProvider

MACHINES = 20
SCOPE = ScopeEntry("github:acme/api", "github", "acme/api", "acme/api")
ITEMS = [
    Item(
        source="github",
        scope_key=SCOPE.key,
        external_id=f"github:PR_{n}",
        kind="pull_request",
        url=f"https://github.com/acme/api/pull/{n}",
        title=f"PR {n}",
        text=f"@dana: change {n}, we adopt tool{n} for service{n % 5}.",
        reference_time=datetime(2026, 3, 1 + n % 28, tzinfo=UTC),
        participants=("github:dana",),
    )
    for n in range(1, 16)
]
PAGES = [ITEMS[0:5], ITEMS[5:10], ITEMS[10:15]]
N = len(ITEMS)


class SlowSource:
    """Three pages of five items, with a pause per page so concurrent runs overlap."""

    name = "github"

    def __init__(self, pause: float = 0.05) -> None:
        self.pause = pause

    async def resolve(self, target: str) -> ScopeEntry:
        return SCOPE

    async def fetch(self, entry: ScopeEntry, cursor: str | None):
        start = int(cursor[1:]) if cursor else 0
        for i in range(start, len(PAGES)):
            await asyncio.sleep(self.pause)
            yield Page(PAGES[i], f"c{i + 1}")


async def _machines(n: int) -> list[Store]:
    get_config.cache_clear()
    return [Store.from_config() for _ in range(n)]


async def _invariants(store: Store) -> dict[str, int]:
    rows = await store._execute(
        "MATCH (doc:TacitDoc) WITH count(doc) AS docs, count(DISTINCT doc.external_id) AS ids"
        " MATCH (c:TacitChunk) WITH docs, ids, count(c) AS chunks"
        " OPTIONAL MATCH (l:TacitLease) WHERE l._fence IS NOT NULL OR l._lock IS NOT NULL"
        " RETURN docs, ids, chunks, count(l) AS stuck_locks"
    )
    return rows[0]


async def test_20_concurrent_syncs_have_one_writer_and_no_duplicates(store: Store):
    from tacit.graph.client import Extractor

    await st.scope_add(store, SCOPE)
    machines = await _machines(MACHINES)
    providers = [FakeProvider() for _ in machines]
    extractors = [Extractor(m, p, max_usd=100) for m, p in zip(machines, providers, strict=True)]
    await extractors[0].setup()  # Graphiti indices once; the others share the graph
    try:
        # Wave 1: everyone starts at once on an empty graph.
        reports = await asyncio.gather(
            *(
                run_sync(m, {"github": SlowSource()}, extractor=ex)
                for m, ex in zip(machines, extractors, strict=True)
            )
        )
        winners = [r for r in reports if r.blocked_by is None]
        llm_runs = [p.calls for p in providers if p.calls]
        inv = await _invariants(store)
        episodes = (
            await store._execute(
                "MATCH (doc:TacitDoc) WHERE doc.episode_uuid IS NOT NULL"
                " WITH count(doc) AS linked MATCH (e:Episodic) RETURN linked, count(e) AS episodes"
            )
        )[0]

        assert len(winners) == 1 and sum(r.blocked_by is not None for r in reports) == 19
        assert winners[0].scopes[0].written == N and winners[0].extracted == N
        assert inv == {"docs": N, "ids": N, "chunks": N, "stuck_locks": 0}
        assert episodes == {"linked": N, "episodes": N}  # one episode per document
        assert len(llm_runs) == 1  # only the winner paid for extraction
        assert await st.cursor_get(store, SCOPE.key) == "c3"

        # Wave 2: everyone again; nothing changed, so nothing is written or paid for.
        before = sum(p.calls for p in providers)
        again = await asyncio.gather(
            *(
                run_sync(m, {"github": SlowSource()}, extractor=ex)
                for m, ex in zip(machines, extractors, strict=True)
            )
        )
        winner2 = [r for r in again if r.blocked_by is None]
        assert len(winner2) == 1
        assert winner2[0].scopes == [] or winner2[0].scopes[0].written == 0
        assert winner2[0].extracted == 0 and sum(p.calls for p in providers) == before
        assert await _invariants(store) == inv

        print(
            f"\n[stress] wave 1: {MACHINES} concurrent syncs -> {len(winners)} writer,"
            f" {MACHINES - len(winners)} backed off; docs={inv['docs']} chunks={inv['chunks']}"
            f" episodes={episodes['episodes']} (expected {N} each); LLM paid by {len(llm_runs)}"
            f" machine. wave 2: written=0, extracted=0, extra LLM calls=0"
        )
    finally:
        for ex in extractors:
            await ex.close()
        await asyncio.gather(*(m.close() for m in machines))


async def test_takeover_storm_fences_stale_writers(store: Store, monkeypatch):
    """Short leases, no background renewal, and writers that stall past the TTL between
    their lease check and their writes: the lease keeps changing hands while 20 machines
    retry. Stale writers must be refused, and the graph must end up exactly right."""
    import tacit.sync as sync_mod

    await st.scope_add(store, SCOPE)
    machines = await _machines(MACHINES)
    real_known = d.known_hashes
    stalls = {"left": 6}

    async def stalling_known(s, ids):  # after the lease check, before the writes
        if stalls["left"] > 0:
            stalls["left"] -= 1
            await asyncio.sleep(1.4)  # longer than the 1 s lease
        return await real_known(s, ids)

    monkeypatch.setattr(sync_mod.d, "known_hashes", stalling_known)
    counts = {"completed": 0, "blocked": 0, "fenced_out": 0, "lease_lost": 0}
    random.seed(7)

    async def machine(m: Store) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            lease = st.Lease(m, ttl_seconds=1, renew_every=3600)  # frozen: never renews
            try:
                r = await run_sync(m, {"github": SlowSource(pause=0.02)}, lease=lease)
            except FencedOut:
                counts["fenced_out"] += 1
            except LeaseLost:
                counts["lease_lost"] += 1
            else:
                if r.blocked_by is None:
                    counts["completed"] += 1
                    return
                counts["blocked"] += 1
            await asyncio.sleep(random.uniform(0.05, 0.3))

    try:
        await asyncio.gather(*(machine(m) for m in machines))
        inv = await _invariants(store)
        tokens = (await store._execute("MATCH (l:TacitLease) RETURN l.token AS t"))[0]["t"]
        print(
            f"\n[stress] takeover storm: {MACHINES} machines, {tokens} lease handovers,"
            f" {counts['fenced_out']} stale writes refused, {counts['lease_lost']} runs"
            f" stopped at a lease check, {counts['blocked']} backed off;"
            f" docs={inv['docs']} chunks={inv['chunks']} (expected {N})"
        )
        assert counts["completed"] == MACHINES
        assert counts["fenced_out"] > 0  # the storm really produced stale writers
        assert inv == {"docs": N, "ids": N, "chunks": N, "stuck_locks": 0}
        assert await st.cursor_get(store, SCOPE.key) == "c3"
    finally:
        await asyncio.gather(*(m.close() for m in machines))
