"""Constraints, indexes and the :TacitMeta schema version (design §5)."""

from . import Store

SCHEMA_VERSION = 1
DIM = 384

STATEMENTS = [
    "CREATE CONSTRAINT tacit_doc_id IF NOT EXISTS FOR (d:TacitDoc) REQUIRE d.external_id IS UNIQUE",
    "CREATE CONSTRAINT tacit_chunk_id IF NOT EXISTS FOR (c:TacitChunk) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT tacit_scope_key IF NOT EXISTS FOR (s:TacitScope) REQUIRE s.key IS UNIQUE",
    "CREATE CONSTRAINT tacit_cursor_key IF NOT EXISTS FOR (c:TacitCursor) REQUIRE c.key IS UNIQUE",
    "CREATE CONSTRAINT tacit_lease_name IF NOT EXISTS FOR (l:TacitLease) REQUIRE l.name IS UNIQUE",
    "CREATE CONSTRAINT tacit_person_key IF NOT EXISTS FOR (p:TacitPerson) REQUIRE p.key IS UNIQUE",
    "CREATE CONSTRAINT tacit_meta_name IF NOT EXISTS FOR (m:TacitMeta) REQUIRE m.name IS UNIQUE",
    "CREATE INDEX tacit_doc_url IF NOT EXISTS FOR (d:TacitDoc) ON (d.url)",
    "CREATE INDEX tacit_access_at IF NOT EXISTS FOR (a:TacitAccess) ON (a.at)",
    (
        "CREATE VECTOR INDEX tacit_chunk_embedding IF NOT EXISTS FOR (c:TacitChunk) ON c.embedding"
        f" OPTIONS {{indexConfig: {{`vector.dimensions`: {DIM},"
        " `vector.similarity_function`: 'cosine'}}"
    ),
    "CREATE FULLTEXT INDEX tacit_chunk_text IF NOT EXISTS FOR (c:TacitChunk) ON EACH [c.text, c.title]",
]


class SchemaMismatch(RuntimeError):
    pass


async def ensure_schema(store: Store, embedding_model: str) -> dict:
    """Idempotent. Refuses a newer schema or a different embedder than the graph has."""
    rows = await store.run_query(
        "read", "schema", "OPTIONAL MATCH (m:TacitMeta {name: 'schema'}) RETURN m {.*} AS meta"
    )
    meta = rows[0]["meta"] if rows and rows[0]["meta"] else None
    if meta:
        if meta["version"] > SCHEMA_VERSION:
            raise SchemaMismatch(
                f"This graph uses schema v{meta['version']}; upgrade tacit (you have v{SCHEMA_VERSION})."
            )
        if meta.get("embedding_model") and meta["embedding_model"] != embedding_model:
            raise SchemaMismatch(
                f"This graph was indexed with {meta['embedding_model']}, but your config uses"
                f" {embedding_model}. Everyone on a team must use the same embedder."
            )
    for stmt in STATEMENTS:
        await store.run_query("write", "schema", stmt, log_params={"stmt": stmt[:80]})
    await store.run_query(
        "write",
        "schema",
        "MERGE (m:TacitMeta {name: 'schema'}) SET m.version = $v, m.dim = $dim,"
        " m.embedding_model = coalesce(m.embedding_model, $model),"
        " m.created_at = coalesce(m.created_at, datetime())",
        {"v": SCHEMA_VERSION, "dim": DIM, "model": embedding_model},
    )
    await store.run_query("read", "schema", "CALL db.awaitIndexes(60)")
    return {"version": SCHEMA_VERSION, "embedding_model": embedding_model}
