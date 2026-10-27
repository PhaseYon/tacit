# Retrieval benchmark, local-first stack (2026-10-27)

- Stack: local-first (Neo4j chunk index: bge-small vectors + full-text, RRF k=60)
- Embedder: `BAAI/bge-small-en-v1.5` · k = 10 · questions: 52
- Demo data: 144 issues/PRs/markdown files; skipped as bot-authored: 5, of which gold sources: 0
- Ingest: 0 new items -> 139 documents, 150 chunks in 0.4 s (issue: 38 docs/38 chunks, markdown_file: 26 docs/33 chunks, pull_request: 75 docs/79 chunks)
- Cost: retrieval $0. Extraction: 124 items with claude-haiku-5-5, 2117 LLM calls, $1.367, 48.2 min -> 746 facts (83 superseded), 213 entities
- Graph facts arm: facts: documents cited by the top Graphiti facts, in fact order. hybrid+facts: RRF (k=60) of the hybrid chunk ranking and the facts ranking, i.e. both layers `tacit search` returns, fused at the document level.
- Scoring: document level by URL; comment/review anchors mapped to their parent PR/issue (strict_recall uses exact URLs)

Recall@k = share of questions with at least one gold document in the top k. Gold coverage = share of each question's gold documents found (mean). MRR = mean reciprocal rank of the first gold document. Strict recall@k counts only exact URL matches (comment anchors can't match). Latency: warm, one query each, includes embedding the query and writing the access-log entry.

| arm | n | recall@5 | recall@10 | gold_coverage | mrr | strict_recall@10 | p50_ms | p95_ms |
|---|---|---|---|---|---|---|---|---|
| vector | 52 | 0.846 | 0.942 | 0.837 | 0.711 | 0.808 | 34.5 | 52.9 |
| keyword | 52 | 0.885 | 0.962 | 0.84 | 0.707 | 0.827 | 16.1 | 22.3 |
| hybrid | 52 | 0.885 | 0.962 | 0.849 | 0.704 | 0.808 | 36.1 | 44.8 |
| facts | 52 | 0.788 | 0.846 | 0.7 | 0.515 | 0.731 | 120.7 | 187.5 |
| hybrid+facts | 52 | 0.885 | 0.962 | 0.889 | 0.605 | 0.846 | 165.2 | 235.3 |

## Determinism

- vector: fingerprint `656bf6e5d057ee8e`, second pass identical
- keyword: fingerprint `c89d74026efa928e`, second pass identical
- hybrid: fingerprint `f7f6658cc3f86772`, second pass identical
- facts: fingerprint `97a73500d2e936bc`, second pass identical
- hybrid+facts: fingerprint `fd927cb6e6d276f8`, second pass identical

## Coverage of gold sources

- Gold URLs: 78, present as a TacitDoc URL exactly: 51
- Gold documents (anchors mapped to parent): 60, present: 60
- Questions with at least one reachable gold document: 52/52 (strict: 46)

| missing URL | why |
|---|---|
| https://github.com/larkspur/api/issues/11#issuecomment-3002000005 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/issues/29#issuecomment-3002000029 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/issues/29#issuecomment-3002000032 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/issues/32#issuecomment-3002000033 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/issues/34#issuecomment-3002000038 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/issues/45#issuecomment-3002000050 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/issues/45#issuecomment-3002000053 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/pull/43#issuecomment-3002000048 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/pull/46#issuecomment-3002000054 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/pull/51#discussion_r1702000015 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/pull/6#pullrequestreview-2402000009 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/api/pull/8#pullrequestreview-2402000011 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/issues/14#issuecomment-3001000009 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/11#issuecomment-3001000007 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/12#issuecomment-3001000008 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/17#pullrequestreview-2401000026 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/20#discussion_r1701000002 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/20#pullrequestreview-2401000030 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/20#pullrequestreview-2401000031 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/23#issuecomment-3001000017 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/23#pullrequestreview-2401000033 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/4#discussion_r1701000001 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/5#pullrequestreview-2401000008 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/5#pullrequestreview-2401000010 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/handbook/pull/8#pullrequestreview-2401000014 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/web/issues/21#issuecomment-3003000019 | comment/review anchor: bundled into its PR/issue (in the parent document) |
| https://github.com/larkspur/web/issues/25#issuecomment-3003000025 | comment/review anchor: bundled into its PR/issue (in the parent document) |

## By question type (recall@k / MRR)

| type | vector | keyword | hybrid | facts | hybrid+facts |
|---|---|---|---|---|---|
| evidence | 1.0 / 0.821 | 1.0 / 0.572 | 1.0 / 0.66 | 0.714 / 0.429 | 1.0 / 0.512 |
| multi-hop | 0.857 / 0.417 | 0.857 / 0.714 | 0.857 / 0.619 | 0.857 / 0.378 | 0.857 / 0.512 |
| what-changed | 0.9 / 0.578 | 0.9 / 0.573 | 0.9 / 0.604 | 0.9 / 0.408 | 1.0 / 0.543 |
| when | 1.0 / 0.533 | 1.0 / 0.672 | 1.0 / 0.529 | 1.0 / 0.457 | 1.0 / 0.533 |
| who | 0.9 / 0.85 | 1.0 / 0.728 | 1.0 / 0.787 | 0.7 / 0.508 | 1.0 / 0.586 |
| why | 1.0 / 0.874 | 1.0 / 0.874 | 1.0 / 0.856 | 0.923 / 0.746 | 0.923 / 0.795 |

## Questions that need history (superseded decisions)

- vector: recall@10 0.931, MRR 0.611
- keyword: recall@10 0.931, MRR 0.6
- hybrid: recall@10 0.931, MRR 0.603
- facts: recall@10 0.897, MRR 0.466
- hybrid+facts: recall@10 0.931, MRR 0.564

## Per question (rank of first gold document, - = not in top k)

| id | type | question | vector | keyword | hybrid | facts | hybrid+facts |
|---|---|---|---|---|---|---|---|
| q01 | what-changed | What do we use for report search today, and why did we move off Elasticsearch? | 5 | 5 | 4 | - | 7 |
| q02 | what-changed | Where is Larkspur hosted? | - | - | - | 4 | 7 |
| q03 | what-changed | Which authentication provider do we use? | 7 | 2 | 3 | 4 | 1 |
| q04 | what-changed | How does the web app manage server state? | 1 | 2 | 1 | 4 | 2 |
| q05 | what-changed | What do we use to render inspection reports to PDF? | 3 | 3 | 3 | 3 | 2 |
| q06 | what-changed | Is Elasticsearch still used anywhere? | 10 | 5 | 8 | 3 | 7 |
| q07 | what-changed | How do we handle fuzzy or misspelled address search? | 1 | 1 | 1 | 3 | 2 |
| q08 | what-changed | Are we building a native iOS or Android app? | 1 | 1 | 1 | 1 | 1 |
| q09 | what-changed | Which ADR is the current decision for report search: ADR-0005 or ADR-0012? | 1 | 1 | 1 | 1 | 1 |
| q10 | why | Do we use GraphQL for the public API? | 1 | 1 | 1 | 6 | 3 |
| q11 | what-changed | How does the PDF worker avoid Chromium cold starts? | 1 | 1 | 1 | 3 | 2 |
| q12 | who | Who decided to use Elasticsearch for search originally? | 1 | 2 | 1 | 4 | 1 |
| q13 | who | Who approved replacing Elasticsearch with Postgres full-text search? | 2 | 3 | 3 | 3 | 3 |
| q14 | who | Who decided our pricing model, and what is it? | 1 | 1 | 1 | - | 4 |
| q15 | who | Who made the call not to build a native iOS app for Harbor & Pine Inspections? | 1 | 1 | 1 | 1 | 1 |
| q16 | who | Who decided to move off Heroku, and who led the migration? | 1 | 1 | 1 | 1 | 1 |
| q17 | who | Who decided against GraphQL? | 1 | 1 | 1 | - | 3 |
| q18 | who | Who approved switching to WorkOS, and who signed off on the cost? | 1 | 4 | 3 | 2 | 2 |
| q19 | who | Who decided how conflicting offline edits are resolved, and how? | 1 | 1 | 1 | 1 | 1 |
| q20 | who | Who needs to approve an ADR? | 1 | 1 | 1 | 1 | 3 |
| q21 | who | Who took over search after Jonah left? | - | 5 | 5 | - | 9 |
| q22 | why | Why did we pick Heroku in the first place? | 1 | 1 | 1 | 1 | 1 |
| q23 | why | Why did we move from Heroku to AWS? | 1 | 1 | 1 | 3 | 2 |
| q24 | why | Why did we originally choose wkhtmltopdf instead of headless Chrome? | 4 | 1 | 2 | 1 | 1 |
| q25 | why | Why did we replace wkhtmltopdf? | 1 | 1 | 1 | 1 | 1 |
| q26 | why | Why Celery rather than RQ or Dramatiq for background jobs? | 1 | 1 | 1 | 1 | 1 |
| q27 | why | Why do new tables use UUIDv7 primary keys? | 1 | 1 | 1 | 1 | 1 |
| q28 | why | Why version the API in the URL path instead of a header? | 1 | 1 | 1 | 1 | 1 |
| q29 | why | Why did we leave Auth0? | 9 | 9 | 8 | - | - |
| q30 | why | Why did we build a PWA instead of native mobile apps? | 1 | 1 | 1 | 1 | 1 |
| q31 | why | Why is API rate limiting done in the app instead of AWS WAF? | 1 | 1 | 1 | 1 | 1 |
| q32 | why | Why did we drop Redux? | 1 | 4 | 2 | 5 | 2 |
| q33 | why | Why do photo originals move to Glacier after 90 days? | 1 | 1 | 1 | 1 | 1 |
| q34 | when | When was Heroku decommissioned? | 2 | 1 | 2 | 1 | 2 |
| q35 | when | When did production search switch to Postgres, and when was Elasticsearch removed? | 6 | 9 | 7 | 2 | 3 |
| q36 | when | When was the search reindex outage and how bad was it? | 2 | 4 | 2 | 3 | 2 |
| q37 | when | When were users migrated from Auth0 to WorkOS? | 1 | 1 | 1 | 4 | 1 |
| q38 | when | When did Stripe billing ship, and what pricing does it implement? | 2 | 1 | 2 | 5 | 3 |
| q39 | evidence | What's the evidence that Postgres full-text search is fast enough for us? | 4 | 4 | 4 | 3 | 4 |
| q40 | evidence | What was the root cause of the December 2025 search outage? | 1 | 2 | 1 | 2 | 1 |
| q41 | evidence | How much money did removing Elasticsearch save? | 1 | 1 | 1 | 1 | 1 |
| q42 | evidence | Did anyone object to Elasticsearch when it was chosen? | 1 | 9 | 6 | 6 | 3 |
| q43 | evidence | What evidence is there that permission filtering in Elasticsearch drifted from Postgres? | 2 | 7 | 5 | 1 | 2 |
| q44 | multi-hop | What did the engineer who proposed Elasticsearch say about it when he left, and what happ… | 6 | 1 | 1 | - | 4 |
| q45 | multi-hop | Which constraint led us to wkhtmltopdf, and which infrastructure change removed it? | 1 | 1 | 1 | 2 | 1 |
| q46 | multi-hop | Which postmortem action item led to an ADR, and what did that ADR decide? | 2 | 2 | 3 | 3 | 2 |
| q47 | multi-hop | ADR-0004 came with a warning about a limitation. Did it come true, and what did we do? | 4 | 2 | 2 | 3 | 3 |
| q48 | multi-hop | What follow-up did the move to Postgres search require, and who raised it? | - | - | - | 7 | - |
| q49 | multi-hop | How is the UUIDv7 decision connected to the offline photo outbox? | 2 | 1 | 2 | 3 | 2 |
| q50 | multi-hop | Did Tomás's 2025 concern about Elasticsearch turn out to matter? | 2 | 1 | 1 | 1 | 1 |
| q51 | evidence | Have we decided to move background jobs off Celery to a Postgres-backed queue? | 1 | 1 | 1 | - | 4 |
| q52 | evidence | Do we use a feature flag vendor? | 1 | 1 | 1 | - | 4 |
