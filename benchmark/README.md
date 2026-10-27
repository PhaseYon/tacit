# Benchmark: hybrid search vs vector-only RAG

Goal (from CLAUDE.md): measure hybrid GraphRAG against vector-only RAG on ~50 "why"
questions, with cost and latency, and **report the numbers as measured**.

Two harnesses live here:

| file | stack | status |
|---|---|---|
| [`bench.py`](bench.py) | local-first: Neo4j chunk index (bge-small vectors + Neo4j full-text, RRF) | **maintained** |
| [`bench_v0.py`](bench_v0.py) | v0 hosted: Postgres + pgvector + Postgres FTS | history only; the `apps/api` code it imports is gone, so it no longer runs |

## How to reproduce (local-first)

```bash
# A throwaway Neo4j. The harness refuses a non-empty database unless you pass --wipe.
docker run -d --rm --name tacit-bench -p 7705:7687 \
  -e NEO4J_AUTH=neo4j/tacit-test-password --tmpfs /data neo4j:5.26
uv run python benchmark/bench.py --neo4j-uri bolt://localhost:7705 --k 10
docker stop tacit-bench
```

It uses a temporary config dir and no keychain, loads the fictional **Larkspur Labs**
company ([`demo/`](../demo/README.md)) through the real sync path (`tacit.demo.load`
replays the demo JSON through the real `GitHubSource`, chunker and local
`BAAI/bge-small-en-v1.5` embedder), then runs the 52 questions in
[`demo/benchmark/questions.jsonl`](../demo/benchmark/questions.jsonl) through each arm:

- **vector**: chunk vector index only, grouped to one result per document.
- **keyword**: Neo4j full-text index only, grouped the same way.
- **hybrid**: exactly what `tacit.search.search` returns (RRF k=60 of both).

The single-signal arms call `tacit.search.search_chunks(..., signals=(...))`, the same
code path as `search` with one signal switched off, so they skip the other signal's work
and their latency is honest. Each arm gets one warm-up query; latency includes embedding
the query and writing the access-log entry that every Tacit read writes. Extraction is
off (no LLM key), so the run costs $0. Results go to `benchmark/results/<UTC date>-local-first.{json,md}`.

The harness also runs every arm a second time on the same graph and records a
fingerprint (a hash of all ranked lists) per arm, so two runs can be compared at a glance.

## Results

k = 10, local `bge-small-en-v1.5`, cost $0. Latency is milliseconds on one laptop (WSL2).

| stack | arm | recall@5 | recall@10 | gold coverage | MRR | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| **local-first** (2026-10-27) | vector-only | 0.846 | 0.942 | 0.837 | 0.711 | ~27-33 | ~40-54 |
| | keyword-only (Neo4j full-text) | 0.885 | 0.962 | 0.840 | 0.707 | ~12-17 | ~14-20 |
| | **hybrid (RRF)** | 0.885 | 0.962 | 0.849 | 0.704 | ~33-36 | ~43-49 |
| | graph facts (Graphiti) | not run: needs an LLM key for extraction | | | | | |
| v0 hosted (2026-10-25) | vector-only (pgvector) | 0.615 | 0.750 | 0.631 | 0.386 | ~40 | ~60 |
| | keyword-only (Postgres FTS) | 0.788 | 0.904 | 0.704 | 0.576 | ~53 | ~75 |
| | hybrid (RRF of both) | 0.731 | 0.865 | 0.713 | 0.545 | ~60 | ~78 |
| | graph facts (Graphiti) | not run | | | | | |

Latency ranges are from three full runs. Retrieval numbers were **identical in all three**
(fresh database each time; fingerprints vector `656bf6e5d057ee8e`, keyword
`c89d74026efa928e`, hybrid `f7f6658cc3f86772`), and each run's second pass matched too.

Full output: [`results/2026-10-27-local-first.md`](results/2026-10-27-local-first.md)
(per-question ranks, by question type, coverage table) and `.json`. v0:
[`results/2026-10-25-fastembed.md`](results/2026-10-25-fastembed.md).

### Coverage: can every gold source be found?

- The demo data has 144 issues, PRs and markdown files. 139 are ingested (139 documents,
  150 chunks, ~15 s). The other 5 are Dependabot PRs, which the GitHub source skips as
  bot-authored; none is a gold source. All 26 markdown files match the default doc globs.
- The questions cite **78 gold URLs**. **51** exist as a document URL exactly. The other
  **27** are comment, review or review-thread anchors (`.../pull/20#pullrequestreview-…`,
  `#issuecomment-…`, `#discussion_r…`). The local-first GitHub source bundles a PR or
  issue with its whole discussion into one document, so those anchors have no document of
  their own. Every one of their parent PRs/issues is present.
- With anchors mapped to their parent, all **60 gold documents** exist, so all 52
  questions are answerable: the recall ceiling is 1.0.
- The headline numbers score that mapping (document level). For transparency the
  results also report **strict recall@10** (exact URLs, anchors can't match):
  vector 0.808, keyword 0.827, hybrid 0.808. 46 of 52 questions have at least one
  gold URL that can match strictly, so the strict ceiling is 0.885.

## What this does and doesn't show

- **The numbers are higher than v0, but the comparison isn't like for like.** The
  biggest change is document granularity. v0 indexed every comment and review as its
  own document (328 documents); the local-first stack has 139, one per PR, issue or doc.
  Fewer, larger documents make a top-10 hit easier, and the gold anchors above count as
  hits when their parent PR comes back (v0 needed the exact comment). Treat the gain as
  "the new shape of the index works at least as well on these questions", not as
  "retrieval got 10–20 points better".
- **Vector-only improved the most** (recall@10 0.750 → 0.942, MRR 0.386 → 0.711). With
  the whole discussion in one document, a question's wording tends to land near some
  chunk of the right PR. It gained 10 questions and lost none.
- **Hybrid is no better than keyword-only here**, and its MRR is about the same as
  either single arm (0.704 vs 0.711 / 0.707). As in v0, equal-weight fusion hasn't
  earned a measurable gain on this set. Hybrid stays the default because it doesn't lose
  anything either, and the keyword arm's strength is likely inflated (next bullet).
- **Same caveat as v0: the same author (an LLM agent) wrote the corpus and the
  questions**, so questions reuse the documents' exact terms. That favours keyword search
  more than real users' questions would. A held-out, paraphrased question set is still
  needed.
- **Keyword search lost one question v0 found, and got slightly worse on a few.** q02
  ("Where is Larkspur hosted?"): ADR-0010 says "hosting", never "hosted". Postgres FTS in
  v0 used English stemming; the Neo4j full-text index uses the default analyzer, which
  doesn't stem, so the only matching term is "larkspur", which is in nearly every
  document. No arm finds q02 now. q48 ("What follow-up did the move to Postgres search
  require?") is missed by every arm in both stacks (v0 also missed it on every arm). We didn't change the analyzer:
  that would be a change to the store made because of one benchmark question.
- **Questions that need history** (29, superseded decisions): recall@10 0.931 on every
  arm, MRR ~0.60. This layer only finds the documents; telling current from superseded
  is the graph layer's job, and that arm hasn't run.
- **The graph-facts arm hasn't run.** It needs Graphiti extraction, which needs an LLM
  key and costs money. Until it runs, this repo makes **no claim** that the graph
  improves answers.
- **Latency:** all arms are well under 100 ms on 150 chunks; keyword-only is cheapest
  (no query embedding). Absolute numbers mean little at this size, and v0's ran on a
  different stack, so the latency columns aren't directly comparable.
- Retrieval-level only: these numbers measure whether the right sources are found, not
  answer quality.
- **Nothing was tuned on these 52 questions.** The search code is the shipped code; the
  only change made for this benchmark was exposing the single-signal arms.

## Next

1. Write a held-out set of questions phrased the way users actually ask (paraphrased, not
   copied from the documents) before tuning anything (fusion weights, analyzer).
2. Run the graph-facts arm with an API key and record the measured $/item.
3. Consider per-comment anchors inside bundled documents, so citations can point at the
   exact comment again (and strict recall can reach 1.0).
