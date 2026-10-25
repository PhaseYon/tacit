"""Chunk + embed documents whose content changed since they were last indexed.

"Never re-process unchanged content": a document is picked up only when
processed_hash IS DISTINCT FROM content_hash, and metadata-only changes (reactions,
approvals) don't change content_hash.
"""

import json
import logging
from dataclasses import dataclass

from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from ..connectors.documents import count_tokens
from ..db import worker_session
from .chunking import chunk_text, context_header
from .embeddings import get_embedder

log = logging.getLogger(__name__)

BATCH = 32


@dataclass(frozen=True)
class IndexResult:
    documents: int
    chunks: int
    tokens: int


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


async def purge_deleted(workspace_id: str) -> None:
    async with worker_session(workspace_id) as s:
        await s.execute(
            text(
                "DELETE FROM document_chunk c USING source_document d"
                " WHERE c.document_id = d.id AND d.deleted_at IS NOT NULL"
            )
        )


async def index_batch(workspace_id: str, batch: int = BATCH) -> IndexResult:
    """Index up to `batch` changed documents in one transaction."""
    embedder = get_embedder()
    async with worker_session(workspace_id) as s:
        docs = (
            await s.execute(
                text(
                    "SELECT d.id, d.container_id, d.kind, COALESCE(d.title, p.title) AS title,"
                    " d.body, d.metadata FROM source_document d"
                    " LEFT JOIN source_document p ON p.id = d.parent_id"
                    " WHERE d.processed_hash IS DISTINCT FROM d.content_hash"
                    " AND d.deleted_at IS NULL"
                    " ORDER BY d.ingested_at LIMIT :n FOR UPDATE OF d SKIP LOCKED"
                ),
                {"n": batch},
            )
        ).all()
        if not docs:
            return IndexResult(0, 0, 0)

        rows: list[dict] = []
        to_embed: list[str] = []
        for d in docs:
            meta = d.metadata if isinstance(d.metadata, dict) else json.loads(d.metadata)
            header = context_header(d.kind, d.title, meta)
            # A PR or issue with an empty body is still findable by its title.
            pieces = chunk_text(d.body) or [d.title or header]
            for i, piece in enumerate(pieces):
                rows.append(
                    {
                        "ws": workspace_id,
                        "cid": d.container_id,
                        "did": d.id,
                        "idx": i,
                        "title": header,
                        "content": piece,
                        "tokens": count_tokens(piece),
                        "model": embedder.model,
                    }
                )
                to_embed.append(f"{header}\n\n{piece}")

        # Model inference is CPU-bound and sync; keep the event loop free.
        vectors = await run_in_threadpool(embedder.embed_documents, to_embed)
        for row, vec in zip(rows, vectors, strict=True):
            row["emb"] = vector_literal(vec)

        ids = [d.id for d in docs]
        await s.execute(
            text("DELETE FROM document_chunk WHERE document_id = ANY(:ids)"), {"ids": ids}
        )
        await s.execute(
            text(
                "INSERT INTO document_chunk (workspace_id, container_id, document_id,"
                " chunk_index, title, content, token_count, embedding, embedding_model)"
                " VALUES (:ws, :cid, :did, :idx, :title, :content, :tokens,"
                " CAST(:emb AS vector), :model)"
            ),
            rows,
        )
        await s.execute(
            text("UPDATE source_document SET processed_hash = content_hash WHERE id = ANY(:ids)"),
            {"ids": ids},
        )
    return IndexResult(len(docs), len(rows), sum(r["tokens"] for r in rows))


async def index_workspace(workspace_id: str, max_batches: int = 50) -> IndexResult:
    await purge_deleted(workspace_id)
    total = IndexResult(0, 0, 0)
    for _ in range(max_batches):
        r = await index_batch(workspace_id)
        total = IndexResult(
            total.documents + r.documents, total.chunks + r.chunks, total.tokens + r.tokens
        )
        if r.documents < BATCH:
            break
    if total.documents:
        log.info(
            "Indexed %s docs (%s chunks, %s tokens) in %s", *total.__dict__.values(), workspace_id
        )
    return total
