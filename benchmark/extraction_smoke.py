"""One real-LLM extraction through Tacit's adapter, recorded for the website and README.

Not a benchmark: it proves our LLM adapter works with a real model end to end and shows
what Graphiti extracts from one decision record, with its cost and time.

    uv run tacit auth anthropic            # once
    docker compose --profile test up -d --wait neo4j-test
    uv run python benchmark/extraction_smoke.py --neo4j-uri bolt://localhost:7688 --wipe

Writes benchmark/extraction/<date>-smoke.json. Uses the throwaway database you point it at.
"""

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_URL = (
    "https://github.com/larkspur/handbook/blob/main/docs/adr/0012-postgres-full-text-search.md"
)
OUT_DIR = Path(__file__).resolve().parent / "extraction"


async def run(args: argparse.Namespace) -> dict:
    from tacit import demo
    from tacit.graph.client import Extractor
    from tacit.index.embeddings import get_embedder
    from tacit.llm import get_provider
    from tacit.store import Store
    from tacit.store import docs as d
    from tacit.store.schema import ensure_schema

    store = Store.from_config()
    try:
        if args.wipe:
            await store._execute("MATCH (n) DETACH DELETE n")
        await ensure_schema(store, get_embedder().model)
        await demo.load(store)
        pending = await d.pending_extraction(store, limit=1000)
        item = next(i for i in pending if i.url == args.url)
        provider = get_provider()
        ex = Extractor(store, provider, max_usd=args.max_usd)
        await ex.setup()
        started = time.perf_counter()
        try:
            episode = await ex.extract(item)
        finally:
            await ex.close()
        seconds = time.perf_counter() - started
        entities = await store._execute(
            "MATCH (:Episodic {uuid: $u})-[:MENTIONS]->(n:Entity)"
            " RETURN n.name AS name, [l IN labels(n) WHERE l <> 'Entity'] AS types ORDER BY name",
            {"u": episode},
        )
        facts = await store._execute(
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE $u IN r.episodes"
            " RETURN r.name AS relation, a.name AS source, b.name AS target, r.fact AS fact,"
            " toString(r.valid_at) AS valid_at ORDER BY relation, fact",
            {"u": episode},
        )
    finally:
        await store.close()
    return {
        "kind": "extraction-smoke",
        "date": datetime.now(UTC).date().isoformat(),
        "note": "Single-item smoke test, not a benchmark.",
        "provider": provider.name,
        "model": provider.model_for("default"),
        "item": {"title": item.title, "url": item.url, "chars": len(item.text)},
        "seconds": round(seconds, 1),
        "cost_usd": round(float(ex.spent), 4),
        "entities": [{"name": e["name"], "types": e["types"]} for e in entities],
        "facts": [{**f, "valid_at": (f["valid_at"] or "")[:10]} for f in facts],
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--neo4j-uri", required=True)
    p.add_argument("--neo4j-password", default="tacit-test-password")
    p.add_argument("--url", default=DEFAULT_URL, help="demo item to extract")
    p.add_argument("--max-usd", type=float, default=0.05)
    p.add_argument("--wipe", action="store_true", help="empty the (throwaway!) database first")
    args = p.parse_args()
    os.environ["TACIT_GRAPH__URI"] = args.neo4j_uri
    os.environ["TACIT_SECRET_NEO4J_PASSWORD"] = args.neo4j_password
    os.environ.setdefault("TACIT_IDENTITY__EMAIL", "benchmark@tacit.local")
    result = asyncio.run(run(args))
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{result['date']}-smoke.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"{result['model']}: {len(result['facts'])} facts, {len(result['entities'])} entities,"
        f" {result['seconds']} s, ${result['cost_usd']} → {out}"
    )


if __name__ == "__main__":
    main()
