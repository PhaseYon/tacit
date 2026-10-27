"""TacitDoc (one per Item) and its TacitChunks (searchable text + embedding)."""

import json
from typing import Any

from ..sources.base import Item
from . import Store


async def known_hashes(store: Store, external_ids: list[str]) -> dict[str, str]:
    """external_id -> content_hash for items already in the graph."""
    if not external_ids:
        return {}
    rows = await store.run_query(
        "read",
        "doc.hashes",
        "MATCH (d:TacitDoc) WHERE d.external_id IN $ids RETURN d.external_id AS id, d.content_hash AS h",
        {"ids": external_ids},
        log_params={"count": len(external_ids)},
    )
    return {r["id"]: r["h"] for r in rows}


async def upsert_doc(
    store: Store, item: Item, chunks: list[str], embeddings: list[list[float]]
) -> None:
    """Replace the doc and all its chunks in one transaction."""
    assert len(chunks) == len(embeddings)
    props = {
        "source": item.source,
        "scope_key": item.scope_key,
        "kind": item.kind,
        "url": item.url,
        "title": item.title,
        "text": item.text,
        "content_hash": item.content_hash,
        "reference_time": item.reference_time,
        "participants": list(item.participants),
        "metadata": json.dumps(item.metadata, sort_keys=True, default=str),
        "ingested_by": store.who,
    }
    await store.run_query(
        "write",
        "doc.upsert",
        "MERGE (d:TacitDoc {external_id: $eid}) SET d += $props, d.updated_at = datetime()"
        " WITH d OPTIONAL MATCH (d)-[:HAS_CHUNK]->(old:TacitChunk) DETACH DELETE old"
        " WITH DISTINCT d UNWIND $chunks AS ch"
        " CREATE (d)-[:HAS_CHUNK]->(:TacitChunk {id: ch.id, idx: ch.idx, text: ch.text,"
        "   embedding: ch.embedding, external_id: $eid, title: $props.title, kind: $props.kind,"
        "   source: $props.source, url: $props.url, reference_time: $props.reference_time})",
        {
            "eid": item.external_id,
            "props": props,
            "chunks": [
                {"id": f"{item.external_id}#{i}", "idx": i, "text": t, "embedding": e}
                for i, (t, e) in enumerate(zip(chunks, embeddings, strict=True))
            ],
        },
        log_params={"external_id": item.external_id, "url": item.url, "chunks": len(chunks)},
    )


async def set_episode(
    store: Store, external_id: str, episode_uuid: str | None, extracted_hash: str | None = None
) -> None:
    await store.run_query(
        "write",
        "doc.episode",
        "MATCH (d:TacitDoc {external_id: $eid}) SET d.episode_uuid = $uuid, d.extracted_hash = $h",
        {"eid": external_id, "uuid": episode_uuid, "h": extracted_hash},
    )


async def pending_extraction(store: Store, limit: int = 50) -> list[Item]:
    """Docs whose current text hasn't been extracted yet, oldest first (Graphiti wants
    episodes in time order)."""
    rows = await store.run_query(
        "read",
        "doc.pending",
        "MATCH (d:TacitDoc) WHERE d.extracted_hash IS NULL OR d.extracted_hash <> d.content_hash"
        " RETURN d {.*} AS d ORDER BY d.reference_time ASC, d.external_id ASC LIMIT $n",
        {"n": limit},
    )
    return [_item(r["d"]) for r in rows]


async def count_pending_extraction(store: Store) -> int:
    rows = await store.run_query(
        "read",
        "doc.pending",
        "MATCH (d:TacitDoc) WHERE d.extracted_hash IS NULL OR d.extracted_hash <> d.content_hash"
        " RETURN count(d) AS n",
    )
    return rows[0]["n"] if rows else 0


def _item(d: dict[str, Any]) -> Item:
    ref = d.get("reference_time")
    return Item(
        source=d["source"],
        scope_key=d["scope_key"],
        external_id=d["external_id"],
        kind=d["kind"],
        url=d["url"],
        title=d["title"],
        text=d["text"],
        reference_time=ref.to_native() if hasattr(ref, "to_native") else ref,
        participants=tuple(d.get("participants") or ()),
        metadata=json.loads(d.get("metadata") or "{}"),
    )


async def episode_of(store: Store, external_id: str) -> str | None:
    rows = await store.run_query(
        "read",
        "doc.episode",
        "MATCH (d:TacitDoc {external_id: $eid}) RETURN d.episode_uuid AS u",
        {"eid": external_id},
    )
    return rows[0]["u"] if rows else None


async def get_doc(store: Store, url_or_id: str, *, tool: str = "get_document") -> dict | None:
    rows = await store.run_query(
        "read",
        tool,
        "MATCH (d:TacitDoc) WHERE d.external_id = $k OR d.url = $k"
        " RETURN d {.external_id, .source, .kind, .url, .title, .text, .reference_time,"
        "           .participants, .metadata, .updated_at, .ingested_by} AS doc LIMIT 1",
        {"k": url_or_id},
    )
    if not rows:
        return None
    doc = rows[0]["doc"]
    doc["metadata"] = json.loads(doc.get("metadata") or "{}")
    return doc


async def docs_for_episodes(store: Store, episode_uuids: list[str]) -> dict[str, dict[str, Any]]:
    """episode uuid -> {url, title, external_id}: how facts get their citations."""
    if not episode_uuids:
        return {}
    rows = await store.run_query(
        "read",
        "doc.cite",
        "MATCH (d:TacitDoc) WHERE d.episode_uuid IN $u"
        " RETURN d.episode_uuid AS ep, d.url AS url, d.title AS title, d.external_id AS id",
        {"u": episode_uuids},
        log_params={"count": len(episode_uuids)},
    )
    return {r["ep"]: {"url": r["url"], "title": r["title"], "external_id": r["id"]} for r in rows}


async def delete_doc(store: Store, url_or_id: str) -> str | None:
    """Remove a doc and its chunks; returns its episode uuid (the caller removes the
    Graphiti episode) or None if not found."""
    rows = await store.run_query(
        "write",
        "doc.delete",
        "MATCH (d:TacitDoc) WHERE d.external_id = $k OR d.url = $k"
        " OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c) WITH d, collect(c) AS cs, d.episode_uuid AS ep"
        " FOREACH (c IN cs | DETACH DELETE c) DETACH DELETE d RETURN ep, true AS found",
        {"k": url_or_id},
    )
    if not rows:
        return None
    return rows[0]["ep"] or ""


async def stats(store: Store) -> dict[str, Any]:
    rows = await store.run_query(
        "read",
        "status",
        "MATCH (d:TacitDoc) WITH d.source AS source, count(*) AS docs,"
        " max(d.updated_at) AS last RETURN source, docs, last ORDER BY source",
    )
    facts = await store.run_query(
        "read", "status", "MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS n"
    )
    return {"sources": rows, "facts": facts[0]["n"] if facts else 0}
