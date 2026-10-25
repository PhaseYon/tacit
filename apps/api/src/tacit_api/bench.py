"""Reproducible retrieval benchmark: hybrid GraphRAG vs vector-only RAG (CLAUDE.md goal).

    uv run python -m tacit_api.bench [--embedder fastembed|openai] [--k 10] [--arms ...]

1. Creates a throwaway database (tacit_bench) with the real roles, grants and RLS.
2. Loads the fictional demo dataset (demo/) and indexes it with the chosen embedder.
3. Runs every question in demo/benchmark/questions.jsonl through each arm as a user who
   can read everything, and scores whether the gold sources come back.
4. Writes benchmark/results/<date>-<embedder>.{json,md}.

Retrieval-level metrics only (no LLM needed, so it's free and deterministic with the
local embedder). The graph arm runs only when GRAPH_ENABLED + an LLM key are set, and
the report says so explicitly when it didn't run.
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parents[4]
QUESTIONS = REPO / "demo" / "benchmark" / "questions.jsonl"
RESULTS = REPO / "benchmark" / "results"
BENCH_DB = "tacit_bench"
SUPERUSER_URL = os.environ.get(
    "BENCH_SUPERUSER_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
USER_ID, WORKSPACE_ID = "u_bench", "org_bench"


@dataclass
class QuestionResult:
    id: str
    type: str
    requires_history: bool
    rank: int | None  # 1-based rank of the first gold source, None if not in top-k
    gold_found: float  # fraction of gold sources in top-k
    latency_ms: float


@dataclass
class ArmResult:
    arm: str
    k: int
    questions: list[QuestionResult] = field(default_factory=list)

    def summary(self, subset: list[QuestionResult] | None = None) -> dict:
        qs = self.questions if subset is None else subset
        if not qs:
            return {}
        lat = sorted(q.latency_ms for q in qs)
        return {
            "n": len(qs),
            "recall@5": round(sum(1 for q in qs if q.rank and q.rank <= 5) / len(qs), 3),
            f"recall@{self.k}": round(sum(1 for q in qs if q.rank) / len(qs), 3),
            "gold_coverage": round(statistics.mean(q.gold_found for q in qs), 3),
            "mrr": round(statistics.mean(1 / q.rank if q.rank else 0 for q in qs), 3),
            "p50_ms": round(lat[len(lat) // 2], 1),
            "p95_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
        }


def _db_url(driver: str, role: str, pw: str) -> str:
    return f"postgresql{driver}://{role}:{pw}@localhost:5432/{BENCH_DB}"


def create_database() -> None:
    with psycopg.connect(SUPERUSER_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {BENCH_DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {BENCH_DB}")
        c.execute(f"REVOKE ALL ON DATABASE {BENCH_DB} FROM PUBLIC")
        c.execute(
            f"GRANT CONNECT ON DATABASE {BENCH_DB}"
            " TO tacit_migrator, tacit_auth, tacit_api, tacit_worker"
        )
        c.execute(f"GRANT CREATE ON DATABASE {BENCH_DB} TO tacit_migrator")
    with psycopg.connect(SUPERUSER_URL.rsplit("/", 1)[0] + f"/{BENCH_DB}", autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        c.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        c.execute("ALTER SCHEMA public OWNER TO tacit_migrator")
        c.execute("GRANT USAGE ON SCHEMA public TO tacit_auth, tacit_api, tacit_worker")
    env = {**os.environ, "ALEMBIC_DATABASE_URL": _db_url("+psycopg", "tacit_migrator", "migrator")}
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=REPO / "apps" / "api",
        env=env,
        check=True,
        capture_output=True,
    )
    with psycopg.connect(_db_url("", "tacit_migrator", "migrator"), autocommit=True) as c:
        c.execute(
            'INSERT INTO "user" (id, name, email, "emailVerified") VALUES (%s, %s, %s, true)',
            (USER_ID, "Benchmark", "bench@example.com"),
        )
        c.execute(
            'INSERT INTO organization (id, name, slug, "createdAt") VALUES (%s, %s, %s, now())',
            (WORKSPACE_ID, "Larkspur (bench)", "bench"),
        )
        c.execute(
            'INSERT INTO member (id, "organizationId", "userId", role, "createdAt")'
            " VALUES ('m_bench', %s, %s, 'owner', now())",
            (WORKSPACE_ID, USER_ID),
        )


def load_questions() -> list[dict]:
    return [json.loads(line) for line in QUESTIONS.read_text().splitlines() if line.strip()]


async def run(args: argparse.Namespace) -> dict:
    # Point the app at the benchmark DB before anything reads settings.
    os.environ["API_DATABASE_URL"] = _db_url("+asyncpg", "tacit_api", "api")
    os.environ["WORKER_DATABASE_URL"] = _db_url("+asyncpg", "tacit_worker", "worker")
    os.environ["EMBEDDING_PROVIDER"] = args.embedder
    from .config import get_settings

    get_settings.cache_clear()
    from .db import dispose_engine, rls_session
    from .demo import load_demo
    from .search.embeddings import get_embedder
    from .search.hybrid import baseline_search, hybrid_search

    get_embedder.cache_clear()
    t0 = time.perf_counter()
    loaded = await load_demo(WORKSPACE_ID, private_access_user_ids=[USER_ID])
    load_s = time.perf_counter() - t0

    questions = load_questions()
    arms: dict[str, ArmResult] = {}
    searchers = {
        "vector": lambda s, q: baseline_search(s, q, "vector", args.k),
        "keyword": lambda s, q: baseline_search(s, q, "keyword", args.k),
        "hybrid": lambda s, q: hybrid_search(s, q, args.k),
    }
    for arm in args.arms:
        if arm not in searchers:
            continue
        result = ArmResult(arm, args.k)
        for q in questions:
            gold = set(q["gold_sources"])
            async with rls_session(USER_ID, WORKSPACE_ID) as s:
                t = time.perf_counter()
                hits = await searchers[arm](s, q["question"])
                ms = (time.perf_counter() - t) * 1000
            urls = [h.url for h in hits]
            rank = next((i + 1 for i, u in enumerate(urls) if u in gold), None)
            found = len(gold & set(urls)) / len(gold) if gold else 0.0
            result.questions.append(
                QuestionResult(q["id"], q["type"], bool(q.get("requires_history")), rank, found, ms)
            )
        arms[arm] = result
    await dispose_engine()

    graph_note = "not run: needs GRAPH_ENABLED=true, Neo4j and an LLM API key (costs money)"
    report = {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "embedder": get_embedder().model,
        "k": args.k,
        "dataset": {
            "questions": len(questions),
            **loaded,
            "load_and_index_seconds": round(load_s, 1),
        },
        "arms": {name: a.summary() for name, a in arms.items()},
        "by_type": {
            name: {
                t: a.summary([q for q in a.questions if q.type == t])
                for t in sorted({q.type for q in a.questions})
            }
            for name, a in arms.items()
        },
        "requires_history": {
            name: a.summary([q for q in a.questions if q.requires_history])
            for name, a in arms.items()
        },
        "graph": graph_note,
        "per_question": {name: [asdict(q) for q in a.questions] for name, a in arms.items()},
        "cost_usd": 0.0
        if args.embedder == "fastembed"
        else "see OpenAI usage (query embeddings only)",
    }
    return report


def to_markdown(r: dict) -> str:
    k = r["k"]
    cols = ["n", "recall@5", f"recall@{k}", "gold_coverage", "mrr", "p50_ms", "p95_ms"]
    lines = [
        f"# Retrieval benchmark ({r['date'][:10]})",
        "",
        f"- Embedder: `{r['embedder']}` · k = {k} · questions: {r['dataset']['questions']}",
        f"- Dataset: {json.dumps({k2: v for k2, v in r['dataset'].items() if k2 != 'questions'})}",
        f"- Cost: {r['cost_usd']}",
        f"- Graph arm: {r['graph']}",
        "",
        "Recall@k = share of questions with at least one gold source in the top k. "
        "Gold coverage = share of all gold sources found. MRR = mean reciprocal rank of the "
        "first gold source. Latency is search only (embedding the query included).",
        "",
        "| arm | " + " | ".join(cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    for arm, s in r["arms"].items():
        lines.append(f"| {arm} | " + " | ".join(str(s.get(c, "")) for c in cols) + " |")
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
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedder", default="fastembed", choices=["fastembed", "openai"])
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--arms", nargs="+", default=["vector", "keyword", "hybrid"])
    args = ap.parse_args()
    create_database()
    report = asyncio.run(run(args))
    RESULTS.mkdir(parents=True, exist_ok=True)
    stem = f"{report['date'][:10]}-{args.embedder}"
    (RESULTS / f"{stem}.json").write_text(json.dumps(report, indent=2, default=str))
    (RESULTS / f"{stem}.md").write_text(to_markdown(report))
    print(to_markdown(report))


if __name__ == "__main__":
    main()
