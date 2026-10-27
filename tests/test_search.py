import ast
import asyncio
import json
import re
from datetime import UTC, datetime

import pytest

from tacit.llm.base import Usage
from tacit.search import format_results, fuse, lucene_query, parse_as_of, search
from tacit.sources.base import Item
from tacit.store import Store
from tacit.store import state as st
from tacit.sync import index_item

from .graph_fake_llm import respond


def _item(n: int, text: str, title: str | None = None, day: int | None = None) -> Item:
    return Item(
        source="github",
        scope_key="github:acme/api",
        external_id=f"github:PR_{n}",
        kind="pull_request",
        url=f"https://github.com/acme/api/pull/{n}",
        title=title or f"PR {n}",
        text=text,
        reference_time=datetime(2026, 3, day or n, tzinfo=UTC),
        participants=("github:dana",),
    )


DOCS = [
    _item(1, "We moved the api cache to Redis because Postgres was too slow under load."),
    _item(2, "Billing events now go through Kafka so retries are durable."),
    _item(3, "The web frontend uses React with server components."),
]


async def _index(store: Store, items: list[Item]) -> None:
    for item in items:
        await index_item(store, item)


# -- pure helpers ------------------------------------------------------------------------


def test_lucene_query_escapes_and_ors_terms():
    assert lucene_query('why did we pick "Redis" (cache)?') == "pick OR redis OR cache"
    assert lucene_query("AND OR NOT") == "and OR or OR not"  # lower-cased: not operators
    assert lucene_query("why") == "why"  # all stopwords: keep them rather than nothing
    assert lucene_query("a+b:c/d~") == "b OR c OR d"


def test_parse_as_of():
    assert parse_as_of(None) is None
    assert parse_as_of("2026-03-01") == datetime(2026, 3, 1, 23, 59, 59, 999999, tzinfo=UTC)
    assert parse_as_of("2026-03-01T10:00:00") == datetime(2026, 3, 1, 10, tzinfo=UTC)
    with pytest.raises(ValueError):
        parse_as_of("last tuesday")


def test_rrf_fusion_order_and_grouping():
    def hit(eid, idx, s):
        return {
            "c": {"id": f"{eid}#{idx}", "external_id": eid, "idx": idx, "text": f"{eid}/{idx}"},
            "s": s,
        }

    vec = [hit("a", 0, 0.9), hit("b", 0, 0.8), hit("c", 0, 0.7), hit("a", 1, 0.6)]
    fts = [hit("b", 0, 5.0), hit("a", 1, 4.0)]
    out = fuse(vec, fts, limit=10)
    # b#0 is 2nd semantic + 1st keyword; a#1 is 4th + 2nd; a's best chunk beats a#0 alone.
    assert [r["external_id"] for r in out] == ["b", "a", "c"]
    assert out[0]["score"] == round(1 / 62 + 1 / 61, 6)
    a = out[1]
    assert a["passages"] == ["a/1", "a/0"]  # best passage first, one result per doc
    assert a["matched_by"] == ["semantic", "keyword"]
    assert out[2]["matched_by"] == ["semantic"]
    # Equal scores break on external_id, never on input order.
    tie = fuse([hit("z", 0, 1.0)], [hit("y", 0, 1.0)], limit=10)
    assert [r["external_id"] for r in tie] == ["y", "z"]


# -- chunk layer against Neo4j -----------------------------------------------------------


async def test_keyword_hit_ranks_first_and_is_logged_without_vectors(store: Store):
    await _index(store, DOCS)
    r = await search(store, "why Kafka?", limit=5, tool="cli.search")
    assert r["results"][0]["url"].endswith("/pull/2")
    top = r["results"][0]
    assert top["matched_by"] == ["semantic", "keyword"]
    assert top["title"] == "PR 2" and top["source"] == "github"
    assert top["kind"] == "pull_request" and top["reference_time"].startswith("2026-03-02")
    assert "Kafka" in top["passages"][0]
    assert r["facts"] == [] and r["facts_available"] is False
    json.dumps(r)  # MCP needs it serializable

    log = await st.access_log(store, grep="cli.search")
    assert len(log) == 1
    params = json.loads(log[0]["params"])
    assert params == {"as_of": None, "limit": 5, "query": "why Kafka?"}
    assert log[0]["result_count"] == 1


async def test_semantic_only_hit_and_one_result_per_doc(store: Store):
    long_text = "\n\n".join(
        f"Section {i}. The Redis cache keeps sessions warm. " + "filler words here " * 120
        for i in range(4)
    )
    await _index(store, [*DOCS, _item(4, long_text, title="Cache design")])
    r = await search(store, "zzqx-unknown-term", limit=10)
    assert r["results"], "vector search always returns neighbours"
    assert all(x["matched_by"] == ["semantic"] for x in r["results"])
    r = await search(store, "redis cache sessions", limit=10)
    ids = [x["external_id"] for x in r["results"]]
    assert len(ids) == len(set(ids)) == 4
    assert ids[0] == "github:PR_4"
    assert 1 <= len(r["results"][0]["passages"]) <= 2


async def test_search_is_deterministic(store: Store):
    await _index(store, DOCS + [_item(n, f"Redis note number {n}.") for n in range(5, 12)])
    first = await search(store, "redis cache", limit=8)
    for _ in range(3):
        assert await search(store, "redis cache", limit=8) == first
    assert format_results(first) == format_results(await search(store, "redis cache", limit=8))


async def test_empty_query_and_empty_graph(store: Store):
    assert (await search(store, "   "))["results"] == []
    r = await search(store, "anything")
    assert r["results"] == [] and r["facts"] == []
    assert "No results." in format_results(r)


# -- fact layer (real Graphiti facts from the fake LLM) ----------------------------------


class SupersedingProvider:
    """The shared fake LLM, plus what Graphiti needs to supersede a fact: edges dated at
    the episode's reference time, and "x uses B instead of A" contradicting "x uses A"."""

    name = "fake"

    def model_for(self, tier):
        return "fake-model"

    async def complete_structured(self, messages, schema, *, tier="default", max_tokens=8192):
        data = respond(messages, schema)
        text = "\n".join(m.content for m in messages)
        name = schema.__name__ if schema else ""
        if name == "ExtractedEdges":
            ref = re.search(r"<REFERENCE_TIME>\s*(\S+)", text)
            for edge in data["edges"]:
                edge["valid_at"] = ref.group(1) if ref else None
        elif name == "EdgeDuplicate":
            new = re.search(r"<NEW FACT>\s*(.+?)\s*</NEW FACT>", text, re.S).group(1)
            m = re.match(r"(\w+) uses (\w+) instead of (\w+)", new)
            olds = [
                ast.literal_eval(x) for x in re.findall(r"\{'idx': \d+, 'fact': '[^']*'\}", text)
            ]
            if m:
                data["contradicted_facts"] = [
                    o["idx"] for o in olds if o["fact"] == f"{m.group(1)} uses {m.group(3)}"
                ]
        return data, Usage("fake", "fake-model", 10, 0, 5, 0)


async def _extract_all(store: Store, items: list[Item]) -> None:
    from tacit.graph.client import Extractor

    ex = Extractor(store, SupersedingProvider(), max_usd=100)
    await ex.setup()
    try:
        for item in items:
            await index_item(store, item)
            await ex.extract(item)
    finally:
        await ex.close()


FACT_DOCS = [
    _item(1, "@dana: adopt Postgres for the api cache.", title="Cache on Postgres"),
    _item(3, "@dana: we switch from Postgres to Redis for the api cache.", title="Move to Redis"),
]


async def test_facts_have_citations_status_and_as_of(store: Store, monkeypatch):
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    await _extract_all(store, FACT_DOCS)

    schema_writes = []

    async def record(self, *a, **kw):
        schema_writes.append(1)

    monkeypatch.setattr(Neo4jDriver, "build_indices_and_constraints", record)
    r = await search(store, "api uses Redis Postgres", limit=10, tool="mcp.search")
    await asyncio.sleep(0)
    assert schema_writes == [], "the read path must not run Graphiti's (unlogged) index setup"
    assert r["facts_available"] is True
    by_fact = {f["fact"]: f for f in r["facts"]}
    redis = by_fact["api uses Redis instead of Postgres"]
    assert redis["status"] == "current" and redis["invalid_at"] is None
    assert redis["valid_at"].startswith("2026-03-03")
    assert [c["url"] for c in redis["citations"]] == ["https://github.com/acme/api/pull/3"]
    assert redis["citations"][0]["title"] == "Move to Redis"
    old = by_fact["api uses Postgres"]
    assert old["status"] == "superseded"
    assert old["valid_at"].startswith("2026-03-01") and old["invalid_at"].startswith("2026-03-03")
    assert old["citations"][0]["url"].endswith("/pull/1")
    json.dumps(r)

    text = format_results(r)
    assert "✗ superseded" in text and "✓ current" in text and "/pull/3" in text

    # As of March 2nd, Postgres was the decision and Redis didn't exist yet.
    past = await search(store, "api uses Redis Postgres", limit=10, as_of="2026-03-02")
    past_facts = {f["fact"] for f in past["facts"]}
    assert "api uses Postgres" in past_facts
    assert "api uses Redis instead of Postgres" not in past_facts
    later = await search(store, "api uses Redis Postgres", limit=10, as_of="2026-03-04")
    assert "api uses Postgres" not in {f["fact"] for f in later["facts"]}

    log = await st.access_log(store, grep="mcp.search")
    tools = {e["tool"] for e in log}
    assert tools == {"mcp.search", "mcp.search.facts"}
    assert all("embedding" not in (e["params"] or "") for e in log)

    # Deterministic with facts too.
    assert await search(store, "api uses Redis Postgres", limit=10) == await search(
        store, "api uses Redis Postgres", limit=10
    )
