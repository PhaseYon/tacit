"""Retrieval benchmark on the local-first stack (design §8.3, §10). Replaces bench_v0.py.

    docker run -d --rm --name tacit-bench -p 7705:7687 \\
      -e NEO4J_AUTH=neo4j/tacit-test-password --tmpfs /data neo4j:5.26
    uv run python benchmark/bench.py --neo4j-uri bolt://localhost:7705 [--k 10] [--wipe]

1. Loads the fictional Larkspur Labs company into an EMPTY Neo4j through the real sync
   path (`tacit.demo.load` -> GitHubSource -> chunking -> local bge-small embeddings).
   Extraction is off (no LLM key), so ingest costs $0.
2. Checks coverage: which gold-source URLs in demo/benchmark/questions.jsonl exist as
   TacitDocs, and why the others don't.
3. Runs the 52 questions through each arm and scores at the document level by URL:
   vector-only, keyword-only and hybrid (`tacit.search.search`, exactly what users get).
4. Runs every arm a second time on the same graph and checks the ranked lists match.
5. Writes benchmark/results/<UTC date>-local-first.{json,md}.

Gold sources that are comment/review anchors (`.../pull/20#pullrequestreview-...`) are
scored against their parent PR/issue, because the local-first GitHub source bundles a
PR or issue with its whole discussion into one document. "Strict" numbers (exact URL,
anchors included) are reported too, so the effect of that mapping is visible.

Retrieval numbers are deterministic (local embedder, stable tie-breaks); only latency
varies between runs.
"""

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urldefrag

REPO = Path(__file__).resolve().parents[1]
QUESTIONS = REPO / "demo" / "benchmark" / "questions.jsonl"
DEMO_DATA = REPO / "demo" / "data" / "github"
RESULTS = REPO / "benchmark" / "results"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
ARMS = {"vector": ("semantic",), "keyword": ("keyword",), "hybrid": None}
# Graph arms need an extracted graph (benchmark/extract_demo.py, then --use-existing).
GRAPH_ARMS = ("facts", "hybrid+facts")
RRF_K = 60
GRAPH_NOTE = (
    "not run: the graph-facts arm needs Graphiti extraction, which needs an LLM API key"
    " (no key was available for this run). No claim is made about the fact layer."
)

# v0 (Postgres + pgvector, hosted stack), 2026-10-25, for the comparison table.
V0 = {
    "vector": {"recall@5": 0.615, "recall@10": 0.75, "gold_coverage": 0.631, "mrr": 0.386},
    "keyword": {"recall@5": 0.788, "recall@10": 0.904, "gold_coverage": 0.704, "mrr": 0.576},
    "hybrid": {"recall@5": 0.731, "recall@10": 0.865, "gold_coverage": 0.713, "mrr": 0.545},
}


# -- metrics (pure) ----------------------------------------------------------------------


def doc_url(url: str) -> str:
    """The document a URL lives in: drop the `#issuecomment-…` / `#pullrequestreview-…`
    anchor, since comments and reviews are part of their PR/issue document."""
    return urldefrag(url)[0]


def gold_docs(gold_sources: list[str]) -> list[str]:
    return list(dict.fromkeys(doc_url(u) for u in gold_sources))


def first_rank(ranked: list[str], gold: set[str]) -> int | None:
    """1-based rank of the first gold URL in `ranked`, None if absent."""
    return next((i for i, u in enumerate(ranked, start=1) if u in gold), None)


def gold_found(ranked: list[str], gold: set[str]) -> float:
    return len(gold & set(ranked)) / len(gold) if gold else 0.0


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank on the sorted values (same convention as v0)."""
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


@dataclass
class QuestionResult:
    id: str
    type: str
    requires_history: bool
    rank: int | None  # first gold document, None if not in top k
    gold_found: float  # share of gold documents in top k
    strict_rank: int | None  # first gold URL matched exactly (anchors not mapped)
    latency_ms: float
    ranked: list[str]


def summarize(qs: list[QuestionResult], k: int) -> dict:
    if not qs:
        return {}
    lat = [q.latency_ms for q in qs]
    return {
        "n": len(qs),
        "recall@5": round(sum(1 for q in qs if q.rank and q.rank <= 5) / len(qs), 3),
        f"recall@{k}": round(sum(1 for q in qs if q.rank) / len(qs), 3),
        "gold_coverage": round(statistics.mean(q.gold_found for q in qs), 3),
        "mrr": round(statistics.mean(1 / q.rank if q.rank else 0 for q in qs), 3),
        f"strict_recall@{k}": round(sum(1 for q in qs if q.strict_rank) / len(qs), 3),
        "p50_ms": round(percentile(lat, 0.5), 1),
        "p95_ms": round(percentile(lat, 0.95), 1),
    }


def fingerprint(qs: list[QuestionResult]) -> str:
    """Hash of every ranked list: equal fingerprints = identical retrieval."""
    blob = json.dumps([[q.id, q.ranked] for q in qs])
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# -- coverage ----------------------------------------------------------------------------


def explain_missing(url: str, present: set[str], demo: dict[str, dict]) -> str:
    """Why a gold URL has no TacitDoc of its own. Checked against the demo data and the
    GitHub source's rules, not guessed."""
    from tacit.sources.github.normalize import matches_doc_globs

    base, frag = urldefrag(url)
    parts = base.split("/")  # https: '' github.com owner repo kind ...
    repo = demo.get(f"{parts[3]}/{parts[4]}") if len(parts) > 4 else None
    if frag:
        where = "in the parent document" if base in present else "parent is ALSO missing"
        return f"comment/review anchor: bundled into its PR/issue ({where})"
    if repo is None:
        return "repository not in the demo data"
    kind = parts[5]
    if kind == "blob":
        path = unquote("/".join(parts[7:]))
        files = {f["path"] for f in repo["markdown_files"]}
        if path not in files:
            return f"file {path!r} is not in the demo data"
        if not matches_doc_globs(path, None):
            return f"{path!r} does not match the default doc globs"
        return "doc matches the globs but was not stored (bug?)"
    number = int(parts[6])
    items = [i for i in repo["issues"] + repo["pull_requests"] if i["number"] == number]
    if not items:
        return f"{kind} #{number} is not in the demo data"
    login = (items[0].get("user") or {}).get("login", "")
    if (items[0].get("user") or {}).get("type") == "Bot" or login.endswith("[bot]"):
        return "authored by a bot (skipped by the GitHub source)"
    if items[0]["html_url"] != base:
        return f"demo data has it at {items[0]['html_url']}"
    return "present in the demo data but not stored (bug?)"


def bot_items(demo: dict[str, dict]) -> list[str]:
    """Issues/PRs the GitHub source skips because a bot opened them (e.g. Dependabot)."""
    from tacit.sources.github.normalize import is_bot

    return sorted(
        i["html_url"]
        for data in demo.values()
        for i in data["issues"] + data["pull_requests"]
        if is_bot(i.get("user"))
    )


# -- run ---------------------------------------------------------------------------------


def configure_env(args: argparse.Namespace) -> None:
    """Point tacit's config at the benchmark Neo4j before anything reads it."""
    os.environ["TACIT_CONFIG_DIR"] = tempfile.mkdtemp(prefix="tacit-bench-cfg-")
    os.environ["TACIT_NO_KEYRING"] = "1"
    os.environ["TACIT_GRAPH__URI"] = args.neo4j_uri
    os.environ["TACIT_SECRET_NEO4J_PASSWORD"] = args.neo4j_password
    os.environ["TACIT_EMBEDDINGS__PROVIDER"] = "fastembed"
    os.environ["TACIT_EMBEDDINGS__MODEL"] = EMBED_MODEL
    os.environ["TACIT_IDENTITY__EMAIL"] = "bench@example.com"


def latest_extraction() -> dict | None:
    """Summary of the extraction run that built the graph (benchmark/extraction/*-demo-*)."""
    files = sorted((REPO / "benchmark" / "extraction").glob("*-demo-*.json"))
    return json.loads(files[-1].read_text()) if files else None


def load_questions() -> list[dict]:
    return [json.loads(line) for line in QUESTIONS.read_text().splitlines() if line.strip()]


def cited_urls(facts: list[dict]) -> list[str]:
    """Documents in the order their facts rank (a fact can cite several)."""
    out: list[str] = []
    for f in facts:
        for c in f.get("citations") or []:
            if c["url"] not in out:
                out.append(c["url"])
    return out


def rrf(*ranked_lists: list[str], k: int = RRF_K) -> list[str]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for i, url in enumerate(ranked):
            scores[url] = scores.get(url, 0.0) + 1 / (k + i + 1)
    return sorted(scores, key=lambda u: (-scores[u], u))


async def run_arm(store, arm: str, questions: list[dict], k: int) -> list[QuestionResult]:
    from tacit.search import _search_facts, search, search_chunks

    async def ranked_urls(query: str) -> list[str]:
        if arm == "facts":
            return cited_urls(await _search_facts(store, query, k, "bench.facts", None, None))[:k]
        if arm == "hybrid+facts":
            out = await search(store, query, limit=k, tool="bench.hybrid+facts")
            chunk_docs = [h["url"] for h in out["results"]]
            return rrf(chunk_docs, cited_urls(out["facts"]))[:k]
        signals = ARMS[arm]
        if signals is None:
            hits = (await search(store, query, limit=k, tool=f"bench.{arm}"))["results"]
        else:
            hits, _ = await search_chunks(store, query, k, f"bench.{arm}", signals=signals)
        return [h["url"] for h in hits]

    await ranked_urls("warm-up: why did we pick this?")  # load the model, warm caches
    out = []
    for q in questions:
        t = time.perf_counter()
        ranked = await ranked_urls(q["question"])
        ms = (time.perf_counter() - t) * 1000
        gold = set(gold_docs(q["gold_sources"]))
        out.append(
            QuestionResult(
                q["id"],
                q["type"],
                bool(q.get("requires_history")),
                first_rank(ranked, gold),
                gold_found(ranked, gold),
                first_rank(ranked, set(q["gold_sources"])),
                round(ms, 1),
                ranked,
            )
        )
    return out


async def run(args: argparse.Namespace) -> dict:
    from tacit import demo
    from tacit.config import get_config
    from tacit.index.embeddings import get_embedder
    from tacit.store import Store
    from tacit.store.schema import ensure_schema

    get_config.cache_clear()
    get_embedder.cache_clear()
    questions = load_questions()
    demo_data = demo.load_data(DEMO_DATA)

    async with Store.from_config() as store:
        await store.driver.verify_connectivity()
        nodes = (await store._execute("MATCH (n) RETURN count(n) AS n"))[0]["n"]
        if nodes and not (args.wipe or args.use_existing):
            sys.exit(
                f"{args.neo4j_uri} is not empty ({nodes} nodes). Use --wipe on a throwaway DB,"
                " or --use-existing for a graph built by benchmark/extract_demo.py."
            )
        if nodes and args.wipe:
            await store._execute("MATCH (n) DETACH DELETE n")
        facts_in_graph = (await store._execute("MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS n"))[
            0
        ]["n"]
        if set(args.arms) & set(GRAPH_ARMS) and not facts_in_graph:
            sys.exit("Graph arms need extracted facts: run benchmark/extract_demo.py first.")
        await ensure_schema(store, get_embedder().model)

        t0 = time.perf_counter()
        report = await demo.load(store, data_dir=DEMO_DATA)
        ingest_s = time.perf_counter() - t0
        assert report.blocked_by is None and all(s.error is None for s in report.scopes)

        counts = await store._execute(
            "MATCH (d:TacitDoc) OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:TacitChunk)"
            " WITH d, count(c) AS chunks"
            " RETURN d.kind AS kind, count(d) AS docs, sum(chunks) AS chunks ORDER BY kind"
        )
        urls = {r["url"] for r in await store._execute("MATCH (d:TacitDoc) RETURN d.url AS url")}

        gold_urls = sorted({u for q in questions for u in q["gold_sources"]})
        missing_exact = [u for u in gold_urls if u not in urls]
        gold_doc_urls = sorted({doc_url(u) for u in gold_urls})
        missing_docs = [u for u in gold_doc_urls if u not in urls]
        reachable = [q["id"] for q in questions if set(gold_docs(q["gold_sources"])) & urls]
        strict_reachable = [q["id"] for q in questions if set(q["gold_sources"]) & urls]
        coverage = {
            "gold_urls": len(gold_urls),
            "gold_urls_present_exactly": len(gold_urls) - len(missing_exact),
            "gold_documents": len(gold_doc_urls),
            "gold_documents_present": len(gold_doc_urls) - len(missing_docs),
            "questions_with_a_reachable_gold_document": len(reachable),
            "questions_with_a_reachable_gold_url_strict": len(strict_reachable),
            "missing": {u: explain_missing(u, urls, demo_data) for u in missing_exact},
        }

        arms, second = {}, {}
        for arm in args.arms:
            arms[arm] = await run_arm(store, arm, questions, args.k)
        for arm in args.arms:  # determinism: same graph, same questions, same lists?
            second[arm] = await run_arm(store, arm, questions, args.k)

    k = args.k
    return {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "stack": "local-first (Neo4j chunk index: bge-small vectors + full-text, RRF k=60)",
        "embedder": EMBED_MODEL,
        "k": k,
        "dataset": {
            "questions": len(questions),
            "demo_items": sum(
                len(x["issues"]) + len(x["pull_requests"]) + len(x["markdown_files"])
                for x in demo_data.values()
            ),
            "items_written": sum(s.written for s in report.scopes),
            "bot_items_skipped": bot_items(demo_data),
            "by_kind": {r["kind"]: {"docs": r["docs"], "chunks": r["chunks"]} for r in counts},
            "documents": sum(r["docs"] for r in counts),
            "chunks": sum(r["chunks"] for r in counts),
            "ingest_seconds": round(ingest_s, 1),
        },
        "cost_usd": 0.0,
        "extraction": "off" if not facts_in_graph else latest_extraction(),
        "facts_in_graph": facts_in_graph,
        "coverage": coverage,
        "scoring": "document level by URL; comment/review anchors mapped to their parent"
        " PR/issue (strict_recall uses exact URLs)",
        "arms": {a: summarize(qs, k) for a, qs in arms.items()},
        "by_type": {
            a: {
                t: summarize([q for q in qs if q.type == t], k)
                for t in sorted({q.type for q in qs})
            }
            for a, qs in arms.items()
        },
        "requires_history": {
            a: summarize([q for q in qs if q.requires_history], k) for a, qs in arms.items()
        },
        "determinism": {
            a: {
                "fingerprint": fingerprint(arms[a]),
                "second_pass_identical": fingerprint(arms[a]) == fingerprint(second[a]),
            }
            for a in arms
        },
        "graph": GRAPH_NOTE
        if not set(arms) & set(GRAPH_ARMS)
        else (
            "facts: documents cited by the top Graphiti facts, in fact order. hybrid+facts:"
            " RRF (k=60) of the hybrid chunk ranking and the facts ranking, i.e. both"
            " layers `tacit search` returns, fused at the document level."
        ),
        "v0_hosted_baseline": V0,
        "per_question": {a: [asdict(q) for q in qs] for a, qs in arms.items()},
    }


# -- report ------------------------------------------------------------------------------


def to_markdown(r: dict, questions: list[dict]) -> str:
    k = r["k"]
    ds, cov = r["dataset"], r["coverage"]
    gold = {doc_url(u) for q in questions for u in q["gold_sources"]}
    cols = ["n", "recall@5", f"recall@{k}", "gold_coverage", "mrr", f"strict_recall@{k}"]
    cols += ["p50_ms", "p95_ms"]
    lines = [
        f"# Retrieval benchmark, local-first stack ({r['date'][:10]})",
        "",
        f"- Stack: {r['stack']}",
        f"- Embedder: `{r['embedder']}` · k = {k} · questions: {ds['questions']}",
        f"- Demo data: {ds['demo_items']} issues/PRs/markdown files; skipped as bot-authored:"
        f" {len(ds['bot_items_skipped'])}, of which gold sources:"
        f" {len(set(ds['bot_items_skipped']) & gold)}",
        f"- Ingest: {ds['items_written']} items -> {ds['documents']} documents,"
        f" {ds['chunks']} chunks in {ds['ingest_seconds']} s"
        f" ({', '.join(f'{kd}: {v["docs"]} docs/{v["chunks"]} chunks' for kd, v in ds['by_kind'].items())})",
        f"- Cost: ${r['cost_usd']} (extraction {r['extraction']})",
        f"- Graph facts arm: {r['graph']}",
        f"- Scoring: {r['scoring']}",
        "",
        "Recall@k = share of questions with at least one gold document in the top k. Gold"
        " coverage = share of each question's gold documents found (mean). MRR = mean"
        " reciprocal rank of the first gold document. Strict recall@k counts only exact URL"
        " matches (comment anchors can't match). Latency: warm, one query each, includes"
        " embedding the query and writing the access-log entry.",
        "",
        "| arm | " + " | ".join(cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    for arm, s in r["arms"].items():
        lines.append(f"| {arm} | " + " | ".join(str(s.get(c, "")) for c in cols) + " |")
    lines.append("| graph facts | not run (needs an LLM key) |" + " |" * (len(cols) - 1))

    lines += ["", "## Determinism", ""]
    for arm, d in r["determinism"].items():
        same = "identical" if d["second_pass_identical"] else "DIFFERENT"
        lines.append(f"- {arm}: fingerprint `{d['fingerprint']}`, second pass {same}")

    lines += [
        "",
        "## Coverage of gold sources",
        "",
        f"- Gold URLs: {cov['gold_urls']}, present as a TacitDoc URL exactly:"
        f" {cov['gold_urls_present_exactly']}",
        f"- Gold documents (anchors mapped to parent): {cov['gold_documents']}, present:"
        f" {cov['gold_documents_present']}",
        f"- Questions with at least one reachable gold document:"
        f" {cov['questions_with_a_reachable_gold_document']}/{ds['questions']}"
        f" (strict: {cov['questions_with_a_reachable_gold_url_strict']})",
        "",
        "| missing URL | why |",
        "|---|---|",
    ]
    for url, why in cov["missing"].items():
        lines.append(f"| {url} | {why} |")

    lines += ["", "## By question type (recall@k / MRR)", ""]
    types = sorted({t for a in r["by_type"].values() for t in a})
    lines.append("| type | " + " | ".join(r["by_type"]) + " |")
    lines.append("|---|" + "---|" * len(r["by_type"]))
    for t in types:
        cells = []
        for arm in r["by_type"]:
            s = r["by_type"][arm].get(t, {})
            cells.append(f"{s.get(f'recall@{k}', '-')} / {s.get('mrr', '-')}")
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    lines += ["", "## Questions that need history (superseded decisions)", ""]
    for arm, s in r["requires_history"].items():
        lines.append(f"- {arm}: recall@{k} {s.get(f'recall@{k}', '-')}, MRR {s.get('mrr', '-')}")

    arms = list(r["per_question"])
    lines += [
        "",
        "## Per question (rank of first gold document, - = not in top k)",
        "",
        "| id | type | question | " + " | ".join(arms) + " |",
        "|---|---|---|" + "---|" * len(arms),
    ]
    by_id = {a: {q["id"]: q for q in r["per_question"][a]} for a in arms}
    for q in questions:
        text = q["question"].replace("|", "\\|")
        text = text if len(text) <= 90 else text[:89] + "…"
        ranks = [by_id[a][q["id"]]["rank"] or "-" for a in arms]
        lines.append(f"| {q['id']} | {q['type']} | {text} | " + " | ".join(map(str, ranks)) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--neo4j-uri", required=True)
    ap.add_argument(
        "--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "tacit-test-password")
    )
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--arms", nargs="+", default=list(ARMS), choices=[*ARMS, *GRAPH_ARMS])
    ap.add_argument(
        "--use-existing",
        action="store_true",
        help="benchmark the graph already in the database (built by extract_demo.py)",
    )
    ap.add_argument("--wipe", action="store_true", help="empty the (throwaway!) database first")
    ap.add_argument("--out", default=None, help="results file stem (default <date>-local-first)")
    args = ap.parse_args()
    configure_env(args)
    report = asyncio.run(run(args))
    RESULTS.mkdir(parents=True, exist_ok=True)
    stem = args.out or f"{report['date'][:10]}-local-first"
    (RESULTS / f"{stem}.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    md = to_markdown(report, load_questions())
    (RESULTS / f"{stem}.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
