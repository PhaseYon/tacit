# Benchmark: hybrid search vs vector-only RAG

Goal (from CLAUDE.md): measure hybrid GraphRAG against vector-only RAG on ~50 "why"
questions, with cost and latency, and **report the numbers as measured**.

## How to reproduce

```bash
pnpm infra:up                       # Postgres (the benchmark creates its own tacit_bench DB)
cd apps/api && uv run python -m tacit_api.bench --embedder fastembed --k 10
```

It creates a throwaway `tacit_bench` database with the real roles and row-level security,
loads the fictional **Larkspur Labs** dataset ([`demo/`](../demo/README.md): 3 repos, 80 PRs,
38 issues, 26 docs incl. 15 ADRs, 5 decisions that were later reversed), indexes it, and
runs the 52 questions in [`demo/benchmark/questions.jsonl`](../demo/benchmark/questions.jsonl)
as a user who can read everything. Results go to `benchmark/results/`. With the local
embedder, two runs give identical retrieval numbers (only latency varies).

## Results (2026-10-25, local `bge-small-en-v1.5`, k = 10, cost $0)

| arm | recall@5 | recall@10 | gold coverage | MRR | p50 ms | p95 ms |
|---|---|---|---|---|---|---|
| vector-only (pgvector) | 0.615 | 0.750 | 0.631 | 0.386 | ~40 | ~60 |
| keyword-only (Postgres FTS) | 0.788 | 0.904 | 0.704 | 0.576 | ~53 | ~75 |
| **hybrid (RRF of both)** | 0.731 | 0.865 | 0.713 | 0.545 | ~60 | ~78 |
| graph facts (Graphiti) | not run | | | | | |

Full per-question output: [`results/2026-10-25-fastembed.md`](results/2026-10-25-fastembed.md)
and `.json`.

## What this does and doesn't show

- **Hybrid clearly beats vector-only RAG** on this set: +11.5 points recall@10, +0.16 MRR.
  The gap is largest on multi-hop and "what changed" questions, where exact names (ADR
  numbers, product names) matter.
- **Keyword-only beat hybrid here**, which we didn't expect. Two likely reasons:
  1. *Vocabulary overlap:* the same author (an LLM agent) wrote the corpus and the
     questions, so questions reuse the documents' exact terms. That favours keyword search
     more than real users' questions would.
  2. *Equal-weight fusion with a small embedder:* bge-small adds noise that pushes some
     correct keyword hits down the fused list.
  We have **not** tuned fusion weights against these 52 questions. Tuning on the same set
  we report on would overstate results. A held-out question set is needed first.
- **Small dataset:** 328 documents. Absolute latency numbers mean little at this size.
- **The graph arm hasn't run.** It needs an LLM key and costs money (estimated ~$0.004 per
  episode on Haiku 5.5, not yet measured). Until it runs, this repo makes **no claim** that
  the graph improves answers.
- Retrieval-level only: these numbers measure whether the right sources are found, not
  answer quality. Answer-level evaluation (with an LLM judge) comes after the graph run.

## Next

1. Write a held-out set of questions phrased the way users actually ask (paraphrased, not
   copied from the documents) before tuning anything.
2. Run the graph arm with an API key and record the measured $/episode.
3. Try weighted RRF and the OpenAI-384 embedder as extra arms.
