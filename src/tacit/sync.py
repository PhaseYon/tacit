"""`tacit sync`: fetch → hash skip → index (chunks + embeddings) → optional extraction.

Runs entirely under the graph's write lease (design §6). If another teammate holds it,
the run exits cleanly and the next one picks up from the shared cursors.

Phase 1 (always, free): for each scope entry, page through the source since its cursor.
Unchanged items (same content_hash) are skipped. Changed ones are chunked, embedded
locally and written as TacitDoc + TacitChunks. The cursor advances only after a page is
fully written.

Phase 2 (only with extraction on and an LLM key): docs whose current text hasn't been
extracted become Graphiti episodes, oldest first, until the per-run budget is spent.
It reads the stored doc text, so turning extraction on later needs no re-fetch.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import get_config
from .index.chunking import chunk_text, context_header
from .index.embeddings import get_embedder
from .sources.base import Item, ScopeEntry, Source, SourceAccessDenied, SourceNotConfigured
from .store import Store
from .store import docs as d
from .store import state as st

log = logging.getLogger(__name__)


LeaseLost = st.LeaseLost
FencedOut = st.FencedOut


@dataclass
class ScopeReport:
    key: str
    fetched: int = 0
    unchanged: int = 0
    written: int = 0
    skipped_reason: str | None = None
    error: str | None = None


@dataclass
class SyncReport:
    blocked_by: str | None = None
    scopes: list[ScopeReport] = field(default_factory=list)
    extracted: int = 0
    extraction_failed: int = 0
    extraction_pending: int = 0
    extraction_note: str | None = None
    spent_usd: float = 0.0
    pruned_log_entries: int = 0


Progress = Callable[[str], None]


async def index_item(store: Store, item: Item, *, fence: st.Lease | None = None) -> int:
    """Chunk, embed (locally) and write one item. Returns the number of chunks."""
    header = context_header(item.kind, item.title, item.metadata)
    chunks = chunk_text(item.text) or [item.title]
    embedder = get_embedder()
    vectors = await asyncio.to_thread(
        embedder.embed_documents, [f"{header}\n\n{c}" for c in chunks]
    )
    await d.upsert_doc(store, item, chunks, vectors, fence=fence)
    return len(chunks)


async def _sync_entry(
    store: Store, lease: st.Lease, entry: ScopeEntry, source: Source, progress: Progress
) -> ScopeReport:
    report = ScopeReport(entry.key)
    cursor = await st.cursor_get(store, entry.key)
    async for page in source.fetch(entry, cursor):
        await lease.ensure("sync")
        report.fetched += len(page.items)
        known = await d.known_hashes(store, [i.external_id for i in page.items])
        for item in page.items:
            if known.get(item.external_id) == item.content_hash:
                report.unchanged += 1
                continue
            await index_item(store, item, fence=lease)
            report.written += 1
        if page.cursor is not None:
            await st.cursor_set(store, entry.key, page.cursor, fence=lease)
        progress(f"{entry.label}: {report.written} new/changed, {report.unchanged} unchanged")
    return report


async def run_sync(
    store: Store,
    sources: dict[str, Source],
    *,
    extractor=None,
    only: list[str] | None = None,
    progress: Progress = lambda _: None,
    lease: st.Lease | None = None,
) -> SyncReport:
    """`sources` holds only sources this user has a token for. `extractor` is a
    tacit.graph.client.Extractor or None (extraction off). `lease` is for tests (a short
    TTL); by default a fresh one with the standard TTL is claimed.

    The lease is renewed in the background while the run is alive, checked again before
    every page and every extraction, and Tacit's own writes carry its fencing token, so a
    run that lost the lease (slept, stalled past the TTL) stops instead of writing."""
    report = SyncReport()
    lease = lease or st.Lease(store)
    got, holder = await lease.claim()
    if not got:
        report.blocked_by = holder
        return report
    lease.start_renewal()
    try:
        for entry in await st.scope_list(store):
            if only and entry.key not in only and entry.source not in only:
                continue
            source = sources.get(entry.source)
            if source is None:
                report.scopes.append(
                    ScopeReport(entry.key, skipped_reason=f"no {entry.source} token")
                )
                continue
            try:
                report.scopes.append(await _sync_entry(store, lease, entry, source, progress))
            except LeaseLost:
                raise
            except (SourceAccessDenied, SourceNotConfigured) as exc:
                report.scopes.append(ScopeReport(entry.key, skipped_reason=str(exc)))
            except Exception as exc:
                log.exception("Sync of %s failed", entry.key)
                report.scopes.append(ScopeReport(entry.key, error=repr(exc)[:500]))

        if extractor is not None:
            await _extract(store, lease, extractor, report, progress)
        report.extraction_pending = await d.count_pending_extraction(store)
        report.pruned_log_entries = await st.prune_access_log(store, get_config().access_log_days)
    finally:
        await lease.stop_renewal()
        await lease.release()
    return report


async def _extract(store: Store, lease: st.Lease, extractor, report: SyncReport, progress):
    from .graph.costs import BudgetExceeded

    failed: set[str] = set()
    while True:
        batch = [
            i
            for i in await d.pending_extraction(store, limit=20 + len(failed))
            if i.external_id not in failed
        ]
        if not batch:
            break
        for item in batch:
            await lease.ensure("extraction")
            try:
                await extractor.extract(item, fence=lease)
                report.extracted += 1
                progress(f"extracted {report.extracted}: {item.title[:60]}")
            except LeaseLost:
                raise
            except BudgetExceeded as exc:
                report.extraction_note = str(exc)
                report.spent_usd = float(extractor.spent)
                return
            except Exception:
                log.exception("Extraction of %s failed", item.external_id)
                failed.add(item.external_id)
                report.extraction_failed += 1
    report.spent_usd = float(extractor.spent)
