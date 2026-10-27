"""Deterministic hybrid search (design §8, §8.3). No LLM: same graph + query, same output.

Two layers, both in the team's Neo4j:

- **Chunks** (always): vector search over `tacit_chunk_embedding` plus full-text search
  over `tacit_chunk_text`, fused with Reciprocal Rank Fusion (k=60) and grouped into one
  result per document with its best passages.
- **Facts** (only once extraction has produced some): Graphiti's hybrid edge search
  (BM25 + cosine, RRF). Each fact carries its validity window, a current/superseded
  status, and citations resolved through the TacitDoc that produced its episodes.

Every database call is logged: chunk queries through `Store.run_query` (query text in
the log, never the vector), Graphiti through `Store.access`. `tool` names the caller
(`cli.search`, `mcp.search`) so `tacit log` shows where a read came from.

Result shape (JSON-serializable):

    {
      "query": str, "as_of": str | None,
      "results": [ {external_id, title, url, source, kind, reference_time, score,
                    matched_by: ["semantic", "keyword"], passages: [str, ...]} ],
      "facts":   [ {fact, relation, status: "current"|"superseded", valid_at, invalid_at,
                    score, citations: [{url, title, external_id}]} ],
      "facts_available": bool,
    }
"""

import asyncio
import re
from datetime import UTC, datetime, time
from typing import Any

from .index.embeddings import get_embedder
from .store import Store
from .store import docs as d

RRF_K = 60
MIN_CANDIDATES = 50
PASSAGES_PER_DOC = 2
MAX_LIMIT = 50

# Common English words that only add noise to keyword ranking. The full-text index uses
# the no-stop-words analyzer, so "why did we ..." would otherwise rank by "why" and "we".
_STOPWORDS = frozenset(
    """a about after all also an and any are as at be been but by can could did do does
    for from had has have how i if in into is it its me my no not of on or our so than
    that the their them then there these they this to us was we were what when where
    which who whom why will with would you your""".split()
)

_CHUNK_QUERY = """
CALL () {
  WITH $vec AS v WHERE v IS NOT NULL
  CALL db.index.vector.queryNodes('tacit_chunk_embedding', $k, v) YIELD node, score
  RETURN collect({c: node {.id, .external_id, .idx, .text, .title, .url, .kind, .source,
                           .reference_time}, s: score}) AS vec
}
CALL () {
  WITH $lucene AS q WHERE q <> ''
  CALL db.index.fulltext.queryNodes('tacit_chunk_text', q, {limit: $k}) YIELD node, score
  RETURN collect({c: node {.id, .external_id, .idx, .text, .title, .url, .kind, .source,
                           .reference_time}, s: score}) AS fts
}
RETURN vec, fts, EXISTS { MATCH ()-[:RELATES_TO]->() } AS has_facts
"""


def lucene_query(query: str) -> str:
    """Terms OR'ed together, so one missing word doesn't zero the result. Only word
    characters survive, which also removes every Lucene operator and special char."""
    terms = [t.lower() for t in re.findall(r"\w+", query)]
    meaningful = [t for t in terms if t not in _STOPWORDS] or terms
    # AND/OR/NOT are operators only in upper case; terms are lower-cased above.
    return " OR ".join(dict.fromkeys(meaningful))


def parse_as_of(as_of: str | None) -> datetime | None:
    """ISO date or datetime. A bare date means the END of that day (UTC), so decisions
    made that day count as valid "as of" it."""
    if not as_of:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of.strip()):
            day = datetime.fromisoformat(as_of.strip()).date()
            return datetime.combine(day, time.max, tzinfo=UTC)
        dt = datetime.fromisoformat(as_of.strip())
    except ValueError as exc:
        raise ValueError(f"as_of must be an ISO date like 2026-03-01 (got {as_of!r})") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso_time(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _ranked(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best first; ties broken on stable keys so results never depend on index order."""
    return sorted(hits, key=lambda h: (-h["s"], h["c"]["external_id"], h["c"]["idx"]))


def fuse(vec: list[dict[str, Any]], fts: list[dict[str, Any]], limit: int) -> list[dict]:
    """RRF over chunks, then one result per document (score = its best chunk's)."""
    chunks: dict[str, dict[str, Any]] = {}
    for signal, hits in (("semantic", vec), ("keyword", fts)):
        for rank, hit in enumerate(_ranked(hits), start=1):
            c = hit["c"]
            entry = chunks.setdefault(c["id"], {"c": c, "score": 0.0, "by": set()})
            entry["score"] += 1.0 / (RRF_K + rank)
            entry["by"].add(signal)

    by_doc: dict[str, list[dict[str, Any]]] = {}
    for entry in chunks.values():
        by_doc.setdefault(entry["c"]["external_id"], []).append(entry)

    results = []
    for eid, entries in by_doc.items():
        entries.sort(key=lambda e: (-e["score"], e["c"]["idx"]))
        best = entries[0]["c"]
        matched = set().union(*(e["by"] for e in entries))
        results.append(
            {
                "external_id": eid,
                "title": best.get("title"),
                "url": best.get("url"),
                "source": best.get("source"),
                "kind": best.get("kind"),
                "reference_time": iso_time(best.get("reference_time")),
                "score": round(entries[0]["score"], 6),
                "matched_by": [m for m in ("semantic", "keyword") if m in matched],
                "passages": [e["c"]["text"] for e in entries[:PASSAGES_PER_DOC]],
            }
        )
    results.sort(key=lambda r: (-r["score"], r["external_id"]))
    return results[:limit]


SIGNALS = ("semantic", "keyword")


async def search_chunks(
    store: Store,
    query: str,
    limit: int,
    tool: str = "search",
    as_of: str | None = None,
    *,
    signals: tuple[str, ...] = SIGNALS,
) -> tuple[list[dict], bool]:
    """The chunk layer. `signals` picks the arms: both (what `search` uses), or only
    "semantic" / only "keyword" (the benchmark's single-signal baselines). A skipped signal
    costs nothing: no query embedding, no index call."""
    vec = None
    if "semantic" in signals:
        vec = await asyncio.to_thread(get_embedder().embed_query, query)
    lucene = lucene_query(query) if "keyword" in signals else ""
    rows = await store.run_query(
        "read",
        tool,
        _CHUNK_QUERY,
        {"k": max(MIN_CANDIDATES, limit * 5), "vec": vec, "lucene": lucene},
        log_params={"query": query, "limit": limit, "as_of": as_of},
    )
    row = rows[0] if rows else {"vec": [], "fts": [], "has_facts": False}
    return fuse(row["vec"] or [], row["fts"] or [], limit), bool(row["has_facts"])


def _graph_reader():
    """A read-only Graphiti (no LLM). Its Neo4j driver schedules index creation when built
    inside a running loop; that is an unlogged write, and extraction already created the
    indexes, so cancel it before it starts."""
    from .graph.client import build_graphiti

    graphiti = build_graphiti()
    task = getattr(graphiti.driver, "_init_task", None)
    if task is not None:
        task.cancel()
    return graphiti


async def _search_facts(
    store: Store, query: str, limit: int, tool: str, as_of: str | None, at: datetime | None
) -> list[dict]:
    from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
    from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters

    from .graph.client import GROUP

    search_filter = None
    if at is not None:
        # Valid at `at`: started on/before it (or unknown) and not ended by it.
        search_filter = SearchFilters(
            valid_at=[
                [DateFilter(date=at, comparison_operator=ComparisonOperator.less_than_equal)],
                [DateFilter(comparison_operator=ComparisonOperator.is_null)],
            ],
            invalid_at=[
                [DateFilter(comparison_operator=ComparisonOperator.is_null)],
                [DateFilter(date=at, comparison_operator=ComparisonOperator.greater_than)],
            ],
        )
    config = EDGE_HYBRID_SEARCH_RRF.model_copy(deep=True)
    config.limit = limit
    groups = [GROUP]
    assert groups, "never call Graphiti with an empty group list (it means all groups)"

    graphiti = _graph_reader()
    try:
        async with store.access(
            "read", f"{tool}.facts", {"query": query, "limit": limit, "as_of": as_of}
        ) as outcome:
            found = await graphiti.search_(
                query, config=config, group_ids=groups, search_filter=search_filter
            )
            outcome["result_count"] = len(found.edges)
    finally:
        await graphiti.close()

    scores = found.edge_reranker_scores
    edges = [
        (e, scores[i] if i < len(scores) else 0.0)
        for i, e in enumerate(found.edges)
        if e.group_id == GROUP
    ]
    episodes = sorted({u for e, _ in edges for u in (e.episodes or [])})
    cites = await d.docs_for_episodes(store, episodes)

    facts = []
    for e, score in edges:
        citations = [cites[u] for u in dict.fromkeys(e.episodes or []) if u in cites]
        if not citations:
            continue  # no stored document vouches for it (e.g. forgotten): don't show it
        facts.append(
            {
                "fact": e.fact,
                "relation": e.name,
                "status": "superseded" if (e.invalid_at or e.expired_at) else "current",
                "valid_at": iso_time(e.valid_at),
                "invalid_at": iso_time(e.invalid_at),
                "score": round(float(score), 6),
                "citations": sorted(citations, key=lambda c: c["external_id"]),
                "_uuid": e.uuid,
            }
        )
    facts.sort(key=lambda f: (-f["score"], f["_uuid"]))
    for f in facts:
        del f["_uuid"]
    return facts[:limit]


async def search(
    store: Store, query: str, *, limit: int = 10, as_of: str | None = None, tool: str = "search"
) -> dict[str, Any]:
    """Chunks (always) plus Graphiti facts (when the graph has any). `as_of` keeps only
    facts valid at that time; a fact's `status` says whether it still holds today."""
    query = query.strip()
    limit = max(1, min(int(limit), MAX_LIMIT))
    at = parse_as_of(as_of)
    out: dict[str, Any] = {
        "query": query,
        "as_of": as_of,
        "results": [],
        "facts": [],
        "facts_available": False,
    }
    if not query:
        return out
    out["results"], has_facts = await search_chunks(store, query, limit, tool, as_of)
    out["facts_available"] = has_facts
    if has_facts:
        out["facts"] = await _search_facts(store, query, limit, tool, as_of, at)
    return out


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def format_results(results: dict[str, Any]) -> str:
    lines: list[str] = []
    header = f'Search: "{results.get("query", "")}"'
    if results.get("as_of"):
        header += f"  (facts as of {results['as_of']})"
    lines += [header, ""]

    facts = results.get("facts") or []
    if facts:
        lines.append("Facts")
        for f in facts:
            window = f"since {(f['valid_at'] or '?')[:10]}"
            if f.get("invalid_at"):
                window = f"{(f['valid_at'] or '?')[:10]} → {f['invalid_at'][:10]}"
            mark = "✓ current" if f["status"] == "current" else "✗ superseded"
            lines.append(f"  [{mark}] {f['fact']}  ({window})")
            for c in f["citations"]:
                lines.append(f"      ↳ {c.get('title') or c['external_id']}  {c['url']}")
        lines.append("")
    elif results.get("facts_available") is False and results.get("results"):
        lines += ["(No extracted facts yet; showing passages. Run `tacit sync --extract`.)", ""]

    docs = results.get("results") or []
    if docs:
        lines.append("Sources")
        for i, r in enumerate(docs, start=1):
            date = (r.get("reference_time") or "")[:10]
            meta = " · ".join(x for x in (r.get("source"), r.get("kind"), date) if x)
            lines.append(f"{i:2}. {r.get('title') or r['external_id']}  [{meta}]")
            lines.append(f"    {r['url']}")
            for p in r["passages"][:1]:
                lines.append(f"    {_clip(p, 300)}")
        lines.append("")
    if not facts and not docs:
        lines.append("No results.")
    return "\n".join(lines).rstrip() + "\n"
