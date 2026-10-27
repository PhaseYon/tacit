# Retrieval benchmark, local-first stack (2026-10-27)

- Stack: local-first (Neo4j chunk index: bge-small vectors + full-text, RRF k=60)
- Embedder: `BAAI/bge-small-en-v1.5` · k = 10 · questions: 52
- Demo data: 144 issues/PRs/markdown files; skipped as bot-authored: 5, of which gold sources: 0
- Ingest: 139 items -> 139 documents, 150 chunks in 14.5 s (issue: 38 docs/38 chunks, markdown_file: 26 docs/33 chunks, pull_request: 75 docs/79 chunks)
- Cost: $0.0 (extraction off)
- Graph facts arm: not run: the graph-facts arm needs Graphiti extraction, which needs an LLM API key (no key was available for this run). No claim is made about the fact layer.
- Scoring: document level by URL; comment/review anchors mapped to their parent PR/issue (strict_recall uses exact URLs)

Recall@k = share of questions with at least one gold document in the top k. Gold coverage = share of each question's gold documents found (mean). MRR = mean reciprocal rank of the first gold document. Strict recall@k counts only exact URL matches (comment anchors can't match). Latency: warm, one query each, includes embedding the query and writing the access-log entry.

| arm | n | recall@5 | recall@10 | gold_coverage | mrr | strict_recall@10 | p50_ms | p95_ms |
|---|---|---|---|---|---|---|---|---|
| vector | 52 | 0.846 | 0.942 | 0.837 | 0.711 | 0.808 | 26.6 | 40.1 |
| keyword | 52 | 0.885 | 0.962 | 0.84 | 0.707 | 0.827 | 12.3 | 13.9 |
| hybrid | 52 | 0.885 | 0.962 | 0.849 | 0.704 | 0.808 | 33.0 | 49.4 |
| graph facts | not run (needs an LLM key) | | | | | | | |

## Determinism

- vector: fingerprint `656bf6e5d057ee8e`, second pass identical
- keyword: fingerprint `c89d74026efa928e`, second pass identical
- hybrid: fingerprint `f7f6658cc3f86772`, second pass identical

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

| type | vector | keyword | hybrid |
|---|---|---|---|
| evidence | 1.0 / 0.821 | 1.0 / 0.572 | 1.0 / 0.66 |
| multi-hop | 0.857 / 0.417 | 0.857 / 0.714 | 0.857 / 0.619 |
| what-changed | 0.9 / 0.578 | 0.9 / 0.573 | 0.9 / 0.604 |
| when | 1.0 / 0.533 | 1.0 / 0.672 | 1.0 / 0.529 |
| who | 0.9 / 0.85 | 1.0 / 0.728 | 1.0 / 0.787 |
| why | 1.0 / 0.874 | 1.0 / 0.874 | 1.0 / 0.856 |

## Questions that need history (superseded decisions)

- vector: recall@10 0.931, MRR 0.611
- keyword: recall@10 0.931, MRR 0.6
- hybrid: recall@10 0.931, MRR 0.603

## Per question (rank of first gold document, - = not in top k)

| id | type | question | vector | keyword | hybrid |
|---|---|---|---|---|---|
| q01 | what-changed | What do we use for report search today, and why did we move off Elasticsearch? | 5 | 5 | 4 |
| q02 | what-changed | Where is Larkspur hosted? | - | - | - |
| q03 | what-changed | Which authentication provider do we use? | 7 | 2 | 3 |
| q04 | what-changed | How does the web app manage server state? | 1 | 2 | 1 |
| q05 | what-changed | What do we use to render inspection reports to PDF? | 3 | 3 | 3 |
| q06 | what-changed | Is Elasticsearch still used anywhere? | 10 | 5 | 8 |
| q07 | what-changed | How do we handle fuzzy or misspelled address search? | 1 | 1 | 1 |
| q08 | what-changed | Are we building a native iOS or Android app? | 1 | 1 | 1 |
| q09 | what-changed | Which ADR is the current decision for report search: ADR-0005 or ADR-0012? | 1 | 1 | 1 |
| q10 | why | Do we use GraphQL for the public API? | 1 | 1 | 1 |
| q11 | what-changed | How does the PDF worker avoid Chromium cold starts? | 1 | 1 | 1 |
| q12 | who | Who decided to use Elasticsearch for search originally? | 1 | 2 | 1 |
| q13 | who | Who approved replacing Elasticsearch with Postgres full-text search? | 2 | 3 | 3 |
| q14 | who | Who decided our pricing model, and what is it? | 1 | 1 | 1 |
| q15 | who | Who made the call not to build a native iOS app for Harbor & Pine Inspections? | 1 | 1 | 1 |
| q16 | who | Who decided to move off Heroku, and who led the migration? | 1 | 1 | 1 |
| q17 | who | Who decided against GraphQL? | 1 | 1 | 1 |
| q18 | who | Who approved switching to WorkOS, and who signed off on the cost? | 1 | 4 | 3 |
| q19 | who | Who decided how conflicting offline edits are resolved, and how? | 1 | 1 | 1 |
| q20 | who | Who needs to approve an ADR? | 1 | 1 | 1 |
| q21 | who | Who took over search after Jonah left? | - | 5 | 5 |
| q22 | why | Why did we pick Heroku in the first place? | 1 | 1 | 1 |
| q23 | why | Why did we move from Heroku to AWS? | 1 | 1 | 1 |
| q24 | why | Why did we originally choose wkhtmltopdf instead of headless Chrome? | 4 | 1 | 2 |
| q25 | why | Why did we replace wkhtmltopdf? | 1 | 1 | 1 |
| q26 | why | Why Celery rather than RQ or Dramatiq for background jobs? | 1 | 1 | 1 |
| q27 | why | Why do new tables use UUIDv7 primary keys? | 1 | 1 | 1 |
| q28 | why | Why version the API in the URL path instead of a header? | 1 | 1 | 1 |
| q29 | why | Why did we leave Auth0? | 9 | 9 | 8 |
| q30 | why | Why did we build a PWA instead of native mobile apps? | 1 | 1 | 1 |
| q31 | why | Why is API rate limiting done in the app instead of AWS WAF? | 1 | 1 | 1 |
| q32 | why | Why did we drop Redux? | 1 | 4 | 2 |
| q33 | why | Why do photo originals move to Glacier after 90 days? | 1 | 1 | 1 |
| q34 | when | When was Heroku decommissioned? | 2 | 1 | 2 |
| q35 | when | When did production search switch to Postgres, and when was Elasticsearch removed? | 6 | 9 | 7 |
| q36 | when | When was the search reindex outage and how bad was it? | 2 | 4 | 2 |
| q37 | when | When were users migrated from Auth0 to WorkOS? | 1 | 1 | 1 |
| q38 | when | When did Stripe billing ship, and what pricing does it implement? | 2 | 1 | 2 |
| q39 | evidence | What's the evidence that Postgres full-text search is fast enough for us? | 4 | 4 | 4 |
| q40 | evidence | What was the root cause of the December 2025 search outage? | 1 | 2 | 1 |
| q41 | evidence | How much money did removing Elasticsearch save? | 1 | 1 | 1 |
| q42 | evidence | Did anyone object to Elasticsearch when it was chosen? | 1 | 9 | 6 |
| q43 | evidence | What evidence is there that permission filtering in Elasticsearch drifted from Postgres? | 2 | 7 | 5 |
| q44 | multi-hop | What did the engineer who proposed Elasticsearch say about it when he left, and what happ… | 6 | 1 | 1 |
| q45 | multi-hop | Which constraint led us to wkhtmltopdf, and which infrastructure change removed it? | 1 | 1 | 1 |
| q46 | multi-hop | Which postmortem action item led to an ADR, and what did that ADR decide? | 2 | 2 | 3 |
| q47 | multi-hop | ADR-0004 came with a warning about a limitation. Did it come true, and what did we do? | 4 | 2 | 2 |
| q48 | multi-hop | What follow-up did the move to Postgres search require, and who raised it? | - | - | - |
| q49 | multi-hop | How is the UUIDv7 decision connected to the offline photo outbox? | 2 | 1 | 2 |
| q50 | multi-hop | Did Tomás's 2025 concern about Elasticsearch turn out to matter? | 2 | 1 | 1 |
| q51 | evidence | Have we decided to move background jobs off Celery to a Postgres-backed queue? | 1 | 1 | 1 |
| q52 | evidence | Do we use a feature flag vendor? | 1 | 1 | 1 |
