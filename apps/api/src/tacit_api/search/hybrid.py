"""Hybrid search: pgvector similarity + Postgres full-text, fused with Reciprocal Rank
Fusion (RRF). Runs as tacit_api, so RLS limits results to what the user may see,
including source permissions. Returns one best chunk per document, with citations.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from .embeddings import get_embedder
from .indexer import vector_literal

RRF_K = 60
CANDIDATES = 50

HYBRID_SQL = text(
    """
    WITH q AS (
      -- OR the query terms: questions rarely contain every word of the answer, and
      -- ts_rank_cd still ranks documents matching more terms higher.
      SELECT CAST(:emb AS vector) AS e,
             to_tsquery('english',
               replace(plainto_tsquery('english', :q)::text, ' & ', ' | ')) AS tq
    ),
    vec AS (
      SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> q.e) AS r
      FROM document_chunk c, q
      ORDER BY c.embedding <=> q.e
      LIMIT :cand
    ),
    fts AS (
      SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.tq) DESC) AS r
      FROM document_chunk c, q
      WHERE c.tsv @@ q.tq
      ORDER BY ts_rank_cd(c.tsv, q.tq) DESC
      LIMIT :cand
    ),
    fused AS (
      SELECT id, sum(1.0 / (:k + r)) AS score,
             bool_or(src = 'vec') AS by_vec, bool_or(src = 'fts') AS by_fts
      FROM (SELECT id, r, 'vec' AS src FROM vec UNION ALL SELECT id, r, 'fts' FROM fts) x
      GROUP BY id
    ),
    best AS (
      SELECT DISTINCT ON (c.document_id) c.document_id, c.id AS chunk_id, c.content,
             f.score, f.by_vec, f.by_fts
      FROM fused f JOIN document_chunk c ON c.id = f.id
      ORDER BY c.document_id, f.score DESC
    )
    SELECT b.document_id::text, b.chunk_id::text, b.score, b.by_vec, b.by_fts,
           d.kind, COALESCE(d.title, p.title) AS title, d.url, d.author_login,
           d.source_created_at, d.metadata->>'state' AS state, sc.name AS container,
           ts_headline('english', b.content, q.tq,
             'MaxFragments=2, MaxWords=35, MinWords=15, StartSel=**, StopSel=**') AS snippet
    FROM best b
    JOIN source_document d ON d.id = b.document_id
    LEFT JOIN source_document p ON p.id = d.parent_id
    JOIN source_container sc ON sc.id = d.container_id
    CROSS JOIN q
    WHERE d.deleted_at IS NULL
    ORDER BY b.score DESC
    LIMIT :limit
    """
)


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    chunk_id: str
    score: float
    matched_by: list[str]
    kind: str
    title: str | None
    url: str
    author: str | None
    created_at: datetime | None
    state: str | None
    container: str
    snippet: str


async def hybrid_search(session: AsyncSession, query: str, limit: int = 10) -> list[SearchHit]:
    query = query.strip()
    if not query:
        return []
    emb = await run_in_threadpool(get_embedder().embed_query, query)
    # With RLS filtering after the index scan, let HNSW keep scanning until it has
    # enough visible rows (pgvector >= 0.8).
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = await session.execute(
        HYBRID_SQL,
        {"emb": vector_literal(emb), "q": query, "k": RRF_K, "cand": CANDIDATES, "limit": limit},
    )
    return [
        SearchHit(
            document_id=r.document_id,
            chunk_id=r.chunk_id,
            score=float(r.score),
            matched_by=[m for m, on in (("semantic", r.by_vec), ("keyword", r.by_fts)) if on],
            kind=r.kind,
            title=r.title,
            url=r.url,
            author=r.author_login,
            created_at=r.source_created_at,
            state=r.state,
            container=r.container,
            snippet=r.snippet,
        )
        for r in rows
    ]


# --- Single-signal baselines (for the benchmark) --------------------------------------

_BASELINE_SQL = """
    WITH q AS (SELECT CAST(:emb AS vector) AS e, {tsq} AS tq),
    ranked AS (
      SELECT c.document_id, c.id AS chunk_id, c.content, {score} AS score
      FROM document_chunk c, q {where}
      ORDER BY {order} LIMIT :cand
    ),
    best AS (
      SELECT DISTINCT ON (document_id) * FROM ranked ORDER BY document_id, score DESC
    )
    SELECT b.document_id::text, b.chunk_id::text, b.score, d.kind,
           COALESCE(d.title, p.title) AS title, d.url, d.author_login, d.source_created_at,
           d.metadata->>'state' AS state, sc.name AS container, left(b.content, 300) AS snippet
    FROM best b
    JOIN source_document d ON d.id = b.document_id
    LEFT JOIN source_document p ON p.id = d.parent_id
    JOIN source_container sc ON sc.id = d.container_id
    WHERE d.deleted_at IS NULL
    ORDER BY b.score DESC LIMIT :limit
"""
_TSQ = "to_tsquery('english', replace(plainto_tsquery('english', :q)::text, ' & ', ' | '))"
VECTOR_SQL = text(
    _BASELINE_SQL.format(
        tsq="NULL::tsquery",
        score="1 - (c.embedding <=> q.e)",
        where="",
        order="c.embedding <=> q.e",
    )
)
KEYWORD_SQL = text(
    _BASELINE_SQL.format(
        tsq=_TSQ,
        score="ts_rank_cd(c.tsv, q.tq)",
        where="WHERE c.tsv @@ q.tq",
        order="ts_rank_cd(c.tsv, q.tq) DESC",
    )
)


async def baseline_search(
    session: AsyncSession, query: str, mode: str, limit: int = 10
) -> list[SearchHit]:
    """mode: "vector" (pgvector cosine only) or "keyword" (Postgres full-text only)."""
    query = query.strip()
    if not query:
        return []
    emb = await run_in_threadpool(get_embedder().embed_query, query)
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    sql = VECTOR_SQL if mode == "vector" else KEYWORD_SQL
    rows = await session.execute(
        sql, {"emb": vector_literal(emb), "q": query, "cand": CANDIDATES, "limit": limit}
    )
    return [
        SearchHit(
            document_id=r.document_id,
            chunk_id=r.chunk_id,
            score=float(r.score),
            matched_by=["semantic" if mode == "vector" else "keyword"],
            kind=r.kind,
            title=r.title,
            url=r.url,
            author=r.author_login,
            created_at=r.source_created_at,
            state=r.state,
            container=r.container,
            snippet=r.snippet,
        )
        for r in rows
    ]
