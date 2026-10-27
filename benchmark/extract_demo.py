"""Extract the whole demo company into graph facts, recording every LLM response.

    uv run tacit auth anthropic              # once (recording run only)
    docker compose --profile test up -d --wait neo4j-test
    uv run python benchmark/extract_demo.py --neo4j-uri bolt://localhost:7688 --wipe

Responses are saved to benchmark/llm-fixtures/<name>/ keyed by a hash of each prompt.
Re-running with --replay uses only those files: same graph, no API key, $0. Without
--replay, prompts with a saved answer are replayed and new ones go to the real model.

Writes benchmark/extraction/<date>-demo.json (counts, cost, time). Leaves the extracted
graph in the database for benchmark/bench.py --graph to search.
"""

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


async def run(args: argparse.Namespace) -> dict:
    from tacit import demo
    from tacit.graph.client import Extractor
    from tacit.index.embeddings import get_embedder
    from tacit.llm import get_provider
    from tacit.llm.replay import ReplayProvider
    from tacit.store import Store
    from tacit.store import docs as d
    from tacit.store.schema import ensure_schema

    fixtures = HERE / "llm-fixtures" / args.fixtures
    provider = (
        ReplayProvider(fixtures)
        if args.replay
        else ReplayProvider(fixtures, record_with=get_provider())
    )
    store = Store.from_config()
    try:
        if args.wipe:
            await store._execute("MATCH (n) DETACH DELETE n")
        await ensure_schema(store, get_embedder().model)
        await demo.load(store)
        total = await d.count_pending_extraction(store)
        ex = Extractor(store, provider, max_usd=args.max_usd)
        await ex.setup()
        started = time.perf_counter()
        done = 0
        failed: set[str] = set()
        try:
            while True:
                batch = await d.pending_extraction(store, limit=10 + len(failed))
                batch = [i for i in batch if i.external_id not in failed]
                if not batch:
                    break
                for item in batch:
                    t = time.perf_counter()
                    try:
                        await ex.extract(item)
                        done += 1
                        print(
                            f"[{done + len(failed)}/{total}] {time.perf_counter() - t:5.1f}s"
                            f" ${float(ex.spent):.3f}  {item.title[:70]}",
                            flush=True,
                        )
                    except Exception as exc:  # keep going; report at the end
                        failed.add(item.external_id)
                        print(f"FAILED {item.title[:60]}: {exc!r}"[:300], flush=True)
                        if "budget" in repr(exc).lower():
                            raise
        finally:
            await ex.close()
        seconds = time.perf_counter() - started
        counts = (
            await store._execute(
                "MATCH ()-[r:RELATES_TO]->() WITH count(r) AS facts"
                " MATCH (n:Entity) WITH facts, count(n) AS entities"
                " MATCH (e:Episodic) RETURN facts, entities, count(e) AS episodes"
            )
        )[0]
        superseded = (
            await store._execute(
                "MATCH ()-[r:RELATES_TO]->() WHERE r.invalid_at IS NOT NULL OR r.expired_at IS NOT NULL"
                " RETURN count(r) AS n"
            )
        )[0]["n"]
    finally:
        await store.close()
    return {
        "kind": "extraction-demo",
        "date": datetime.now(UTC).date().isoformat(),
        "mode": "replay" if args.replay else "record",
        "model": get_provider().model_for("default") if not args.replay else "replayed",
        "fixtures": f"benchmark/llm-fixtures/{args.fixtures}",
        "items_pending": total,
        "items_extracted": done,
        "items_failed": len(failed),
        "minutes": round(seconds / 60, 1),
        "cost_usd": round(float(ex.spent), 3),
        "llm_calls": len(provider.calls),
        **counts,
        "facts_superseded": superseded,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--neo4j-uri", required=True)
    p.add_argument("--neo4j-password", default="tacit-test-password")
    p.add_argument("--fixtures", default="demo-haiku-5-5")
    p.add_argument("--replay", action="store_true", help="use saved responses only ($0)")
    p.add_argument("--max-usd", type=float, default=3.0)
    p.add_argument("--wipe", action="store_true", help="empty the (throwaway!) database first")
    args = p.parse_args()
    os.environ["TACIT_GRAPH__URI"] = args.neo4j_uri
    os.environ["TACIT_SECRET_NEO4J_PASSWORD"] = args.neo4j_password
    os.environ.setdefault("TACIT_IDENTITY__EMAIL", "benchmark@tacit.local")
    result = asyncio.run(run(args))
    out = HERE / "extraction" / f"{result['date']}-demo-{result['mode']}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
