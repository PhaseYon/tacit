# Retrieval benchmark (2026-10-25)

- Embedder: `BAAI/bge-small-en-v1.5` · k = 10 · questions: 52
- Dataset: {"installation_id": 11966873620, "repos": {"larkspur/handbook": {"container_id": "76fd35cb-7df7-461d-b9e1-2b4a8968b7b5", "visibility": "public", "parents": 28, "issue_comments": 21, "review_comments": 2, "reviews": 19, "markdown": 20}, "larkspur/api": {"container_id": "972c23bb-e344-4236-a1d9-81fcde4eb4a3", "visibility": "private", "parents": 57, "issue_comments": 71, "review_comments": 15, "reviews": 22, "markdown": 5}, "larkspur/web": {"container_id": "a32dfd7c-25d8-405f-922f-d7ff1435fd70", "visibility": "private", "parents": 28, "issue_comments": 28, "review_comments": 5, "reviews": 6, "markdown": 1}}, "indexed": {"documents": 328, "chunks": 334, "tokens": 17650}, "load_and_index_seconds": 48.5}
- Cost: 0.0
- Graph arm: not run: needs GRAPH_ENABLED=true, Neo4j and an LLM API key (costs money)

Recall@k = share of questions with at least one gold source in the top k. Gold coverage = share of all gold sources found. MRR = mean reciprocal rank of the first gold source. Latency is search only (embedding the query included).

| arm | n | recall@5 | recall@10 | gold_coverage | mrr | p50_ms | p95_ms |
|---|---|---|---|---|---|---|---|
| vector | 52 | 0.615 | 0.75 | 0.631 | 0.386 | 56.0 | 86.6 |
| keyword | 52 | 0.788 | 0.904 | 0.704 | 0.576 | 54.1 | 73.9 |
| hybrid | 52 | 0.731 | 0.865 | 0.713 | 0.545 | 67.3 | 85.6 |

## By question type (recall@k / MRR)

| type | vector | keyword | hybrid |
|---|---|---|---|
| evidence | 0.571 / 0.375 | 0.714 / 0.505 | 0.714 / 0.518 |
| multi-hop | 0.429 / 0.088 | 0.857 / 0.431 | 0.714 / 0.302 |
| what-changed | 0.7 / 0.347 | 0.9 / 0.767 | 0.9 / 0.612 |
| when | 1.0 / 0.495 | 1.0 / 0.617 | 1.0 / 0.725 |
| who | 0.8 / 0.55 | 0.9 / 0.342 | 0.9 / 0.498 |
| why | 0.923 / 0.414 | 1.0 / 0.712 | 0.923 / 0.606 |

## Questions that need history (superseded decisions)

- vector: recall@10 0.69, MRR 0.297
- keyword: recall@10 0.897, MRR 0.513
- hybrid: recall@10 0.828, MRR 0.461
