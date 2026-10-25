# Demo dataset: Larkspur Labs

A made-up startup whose engineering history Tacit can sync and search. It's built to
answer the questions Tacit exists for: *why did we do X, who decided, and where's the
evidence?* That includes decisions that were **reversed later**, where the right answer is
the current decision plus its history.

Everything here is fictional: the company, the people, the customers and the numbers. Real
public technologies (Postgres, Elasticsearch, Stripe, WorkOS, …) appear by name. No real
team's data is included.

## The company

**Larkspur Labs** builds **Larkspur**, an app for home and property inspectors. Inspectors
capture an inspection on their phone (often offline, in basements and crawl spaces),
generate a PDF report for their client, and search their past reports by address, client
or defect. Firms pay per inspector seat through Stripe.

The story runs from **March 2025 to September 2026**. The team grows from three people to
eight, moves from Heroku to AWS, and replaces four of its early architecture decisions.

## Cast

| GitHub login | Name | Role | Around |
|---|---|---|---|
| `maya-chen` | Maya Chen | CTO & co-founder; approves ADRs | whole story |
| `ines-duarte` | Inês Duarte | CEO & co-founder; pricing, customers, vendor cost sign-off | whole story |
| `tomas-rivera` | Tomás Rivera | Staff backend engineer; API, Celery, billing, PDFs | whole story |
| `jonah-blake` | Jonah Blake | Founding engineer; proposed Elasticsearch, Auth0, wkhtmltopdf | left Oct 2025 |
| `sam-whitfield` | Sam Whitfield | Frontend lead | whole story |
| `priya-natarajan` | Priya Natarajan | Backend engineer, search & data | from May 2025 |
| `lena-park` | Lena Park | Frontend & offline (PWA) engineer | from Jun 2025 |
| `kofi-mensah` | Kofi Mensah | Platform engineer; AWS migration | from Sep 2025 |
| `ravi-kapoor` | Ravi Kapoor | Security & backend engineer; WorkOS, rate limiting | from Jan 2026 |

Bots (their content must be filtered out): `dependabot[bot]`, `github-actions[bot]`,
`codecov[bot]`.

Fictional customers mentioned in threads: Brightwell Surveys, Harbor & Pine Inspections.

## Repositories

| Repo | Visibility | What's in it |
|---|---|---|
| `larkspur/handbook` | **public** | Engineering handbook, ADR-0001 to ADR-0015, code review rules, on-call guide, tech radar. Every ADR arrives through a PR with reviews. |
| `larkspur/api` | private | FastAPI backend, Celery workers, Terraform. Implementation PRs, incident issue, postmortem, runbooks. |
| `larkspur/web` | private | Next.js web app and offline PWA. Frontend PRs and product discussions. |

Evidence is spread out on purpose: the *what* is often in a public ADR, while the *why* or
the numbers sit in a private PR, review comment, issue thread or postmortem. A user without
access to the private repos should only ever get handbook results.

## Decision timeline

| # | Date | Decision | Proposed / decided by | Evidence | Status |
|---|---|---|---|---|---|
| 1 | 2025-03-10 | Record decisions as ADRs (ADR-0001) | maya-chen | handbook | current |
| 2 | 2025-03-14 | Python + FastAPI + Postgres (ADR-0002) | maya-chen; approved tomas-rivera, jonah-blake | handbook | current |
| 3 | 2025-03-24 | **Host on Heroku** (ADR-0003) | tomas-rivera; approved maya-chen | handbook, api | **superseded by #13** |
| 4 | 2025-04-08 | **Auth0 for authentication** (ADR-0004) | jonah-blake; approved maya-chen, tomas-rivera | handbook, api, web | **superseded by #17** |
| 5 | 2025-04-22 | **Elasticsearch for report search** (ADR-0005) | jonah-blake; approved maya-chen (tomas-rivera objected) | handbook, api | **superseded by #16** |
| 6 | 2025-04-30 | **Redux Toolkit for frontend state** (ADR-0006) | sam-whitfield; approved maya-chen | handbook, web | **superseded by #15** |
| 7 | 2025-05-20 | Celery + Redis for background jobs | tomas-rivera; approved maya-chen | api | current (open discussion about a Postgres queue, Sept 2026) |
| 8 | 2025-06-10 | **wkhtmltopdf for PDF rendering** | jonah-blake; approved tomas-rivera | api | **superseded by #14** |
| 9 | 2025-06-17 | `/v1` URL-path API versioning (ADR-0007) | tomas-rivera; approved maya-chen, sam-whitfield | handbook | current |
| 10 | 2025-07-08 | Per-seat pricing, 40 reports/seat, Stripe Billing | ines-duarte decided; tomas-rivera built it | api | current |
| 11 | 2025-08-05 | Offline-first PWA, no native apps (ADR-0008) | lena-park; approved maya-chen, sam-whitfield | handbook, web | current (re-confirmed 2026-04-28 by ines-duarte) |
| — | 2025-09-12 | GraphQL for the public API **rejected** | sam-whitfield proposed; maya-chen closed | handbook | rejected |
| 12 | 2025-10-02 | UUIDv7 primary keys (ADR-0009) | priya-natarajan; approved tomas-rivera, maya-chen | handbook, web | current |
| 13 | 2025-11-18 | Move to AWS ECS Fargate + RDS (ADR-0010) | kofi-mensah; approved maya-chen, tomas-rivera | handbook, api | current |
| — | 2025-12-04 | Search reindex outage (18% of reports unsearchable) | — | api (issue + postmortem) | trigger for #16 |
| 14 | 2026-01-20 | Replace wkhtmltopdf with Playwright/Chromium | tomas-rivera; approved sam-whitfield, maya-chen | api, web | current |
| 15 | 2026-02-10 | TanStack Query + Zustand (ADR-0011) | lena-park; approved sam-whitfield | handbook, web | current |
| 16 | 2026-03-17 | Postgres full-text search replaces ES (ADR-0012) | priya-natarajan; approved maya-chen, tomas-rivera | handbook, api | current |
| 17 | 2026-05-20 | WorkOS for auth, SSO and SCIM (ADR-0013) | ravi-kapoor; approved maya-chen; cost ines-duarte | handbook, api, web | current |
| 18 | 2026-06-16 | Per-field last-writer-wins for offline edit conflicts | maya-chen decided | web | current |
| 19 | 2026-06-30 | App-level Redis token-bucket rate limiting | ravi-kapoor; approved tomas-rivera | api | current |
| 20 | 2026-08-11 | 7-year retention, originals to Glacier after 90 days (ADR-0014) | ravi-kapoor; approved ines-duarte, maya-chen | handbook, api | current |
| 21 | 2026-09-08 | pg_trgm fuzzy address search, amends ADR-0012 (ADR-0015) | priya-natarajan; approved tomas-rivera | handbook, api | current |

**Reversed decisions** (the old ADR says `Superseded by`, the new one says `Supersedes`):

- Heroku (ADR-0003) → AWS ECS Fargate (ADR-0010): cost, memory limits, VPC peering.
- Auth0 (ADR-0004) → WorkOS (ADR-0013): SAML/SCIM pricing for enterprise customers.
- Elasticsearch (ADR-0005) → Postgres FTS (ADR-0012): 2.1M not 10M reports, sync incidents,
  duplicated permissions, $1,380/month.
- Redux Toolkit (ADR-0006) → TanStack Query + Zustand (ADR-0011): mostly server cache, boilerplate.
- wkhtmltopdf → Playwright (no ADR; PRs and an issue only): unmaintained, no CSS grid, CVEs,
  and the 1 GB Heroku memory limit that motivated it was gone.

## Files

```
demo/
  story/             hand-written story (Python DSL): cast, one module per repo, questions
  generate.py        story -> data/github/*.json + benchmark/questions.jsonl (deterministic)
  data/github/       one JSON file per repo, in GitHub REST API shapes
  benchmark/questions.jsonl
  validate.py        referential-integrity checks
```

Each `data/github/<repo>.json` has `repository`, `issues`, `pull_requests` (full PR
objects with `merged_at`, labels, head/base), `pull_request_reviews` (with `state`, the
source of `approved_by`), `issue_comments`, `review_comments` and `markdown_files`
(`path`, git blob `sha`, `content`). The objects use the same fields as the GitHub API, so
the connector's normalizers consume them unchanged.

Ids, numbers and URLs are derived from the story, so **edit the story and regenerate**
rather than editing the JSON:

```bash
python3 demo/generate.py      # rewrites data/ and benchmark/ byte-for-byte deterministically
python3 demo/validate.py      # must print "0 problems"
```

## Loading it

```bash
pnpm demo:load --workspace <slug-or-id> --grant-all-members
```

This creates a (suspended) fake GitHub installation for the `larkspur` account, the three
repos, normalizes and upserts every document through the regular connector code, records
PR approvals, grants private-repo access, and indexes everything for hybrid search. It
makes no network or LLM calls. Running it again changes nothing except refreshing the ACL
timestamp.

- `--grant-all-members` gives every current workspace member access to the two private
  repos. Without it, members only see the public handbook (useful to demo permission
  filtering). Re-run it after inviting someone.
- Access data counts as fresh for 6 hours (the same rule as real GitHub ACL sync). After
  that, private demo docs disappear from search until you run the loader again.
- The installation is suspended so the scheduled reconcile and ACL jobs never call the
  real GitHub API with a fake installation id.

From Python: `await tacit_api.demo.load_demo(workspace_id, private_access_user_ids=[...])`.

## Benchmark questions

`benchmark/questions.jsonl` has 52 questions, one JSON object per line:

| Field | Meaning |
|---|---|
| `id` | `q01` … |
| `question` | Natural-language question |
| `type` | `why`, `who`, `when`, `what-changed`, `evidence` or `multi-hop` |
| `gold_answer` | Short reference answer |
| `gold_sources` | `html_url`s of the documents that hold the evidence (all exist in the data) |
| `requires_history` | `true` when an older, superseded decision exists, so the answer must be the current one or explain the change |
| `access` | `public` if all evidence is in the handbook, otherwise `private` |

Mix: 10 `what-changed` (current decision vs. a superseded one), 10 `who`, 13 `why`,
5 `when`, 7 `evidence`, 7 `multi-hop`; 29 need history; 14 are answerable from public data
alone. Two questions (q51, q52) ask about things that are still undecided, to catch
systems that invent a decision.

The benchmark runner (hybrid GraphRAG vs. vector-only RAG, with cost and latency) comes
later; this file is its input.

## Adding Slack and Notion later

The story DSL and generator are source-agnostic at the top: add `story/slack.py` /
`story/notion.py` with the same kind of keyed items (channels and messages, pages), emit
`data/slack/*.json` and `data/notion/*.json` in those APIs' shapes, and let questions
reference them with the same `"source:key"` refs. Good candidates already hinted at in the
GitHub data: the Heroku cost discussion, the pricing call, the native-app customer request,
and the incident channel for the 2025-12-04 outage.
