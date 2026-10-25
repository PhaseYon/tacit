"""larkspur/api: the private backend repo (FastAPI, Celery, infra)."""

from .model import PR, Doc, Issue, Repo, approve, c, comment_review, rc, request_changes

HB = "https://github.com/larkspur/handbook/blob/main/docs/adr"

README = f"""
# larkspur/api

Backend for Larkspur: the `/v1` HTTP API, Celery workers and infrastructure (Terraform).

## Stack (current)

- Python 3.12, FastAPI, SQLAlchemy 2, Alembic ([ADR-0002]({HB}/0002-python-fastapi-postgres.md))
- PostgreSQL on RDS; report search is Postgres full-text search plus pg_trgm
  ([ADR-0012]({HB}/0012-postgres-full-text-search.md), [ADR-0015]({HB}/0015-pg-trgm-fuzzy-address-search.md))
- Celery + Redis for background jobs (PDFs, emails, webhooks)
- PDFs rendered with Playwright (headless Chromium)
- Auth via WorkOS ([ADR-0013]({HB}/0013-workos-for-sso-and-auth.md))
- Runs on ECS Fargate ([ADR-0010]({HB}/0010-aws-ecs-fargate-hosting.md)); Terraform in `infra/`

## Local development

```bash
docker compose up -d postgres redis
uv sync
uv run alembic upgrade head
uv run uvicorn larkspur.main:app --reload
uv run celery -A larkspur.worker worker -Q default,pdf
```

See `docs/architecture.md` for how the pieces fit together.
"""

ARCHITECTURE = f"""
# Architecture

Last reviewed 2026-09-12 by @tomas-rivera.

```
PWA / web (Next.js) ──► ALB ──► api (FastAPI, ECS Fargate) ──► RDS Postgres
                                     │                             ▲
                                     └─► Redis (ElastiCache) ◄── workers (Celery)
                                                                   │
                                                          S3 (photos, PDFs)
```

## Services

- **api**: the `/v1` REST API ([ADR-0007]({HB}/0007-url-path-api-versioning.md)). Stateless;
  scales on CPU.
- **worker-default**: emails, Stripe webhooks, outgoing customer webhooks.
- **worker-pdf**: renders inspection reports to PDF with Playwright + Chromium, keeping a
  warm browser pool (4 GB tasks).

## Search

Report search runs in Postgres: a weighted `tsvector` column (`reports.search_tsv`) with a
GIN index, plus a trigram index on the normalized address for fuzzy matching. There is no
Elasticsearch any more; it was removed in April 2026 (ADR-0012).

## Auth

WorkOS AuthKit handles sign-in; enterprise customers connect their IdP via WorkOS SSO and
provision users with Directory Sync (SCIM). The API verifies WorkOS session JWTs.

## Billing

Stripe Billing, per-seat subscriptions with a monthly report allowance. See `docs/billing.md`.

## Data retention

Reports and photos are kept 7 years; photo originals move to Glacier Instant Retrieval after
90 days ([ADR-0014]({HB}/0014-photo-and-report-retention.md)).
"""

POSTMORTEM = """
# Postmortem: report search missing ~18% of reports (2025-12-04)

- Author: @priya-natarajan
- Reviewers: @maya-chen, @kofi-mensah
- Severity: SEV-2 (degraded core feature, no data loss)

## Summary

On 2025-12-04 between 02:10 and 11:20 UTC, about 18% of reports (roughly 340k) did not
appear in search results. Reports themselves were intact in Postgres; only the
Elasticsearch index was incomplete.

## Timeline (UTC)

- 02:00 Nightly full reindex starts (needed after the custom-field mapping change).
- 02:10 The reindex job swaps the `reports` alias to the new index **before** the bulk
  load finished, because the bulk step ran in a separate Celery task and the swap task
  didn't wait for it.
- 03:40 The bulk task hits Elastic Cloud's disk watermark on the 2-node cluster and stalls.
- 08:55 Brightwell Surveys reports missing results to support.
- 09:20 @priya-natarajan opens the incident issue; @kofi-mensah scales the cluster to 3 nodes.
- 11:20 Bulk load completes after a manual restart; search is complete again.

## Root cause

The alias swap and the bulk load were not ordered. A second contributing factor: the
cluster had no headroom for a full reindex, which only happens a few times a year.

## What went well

Postgres was never affected; exports and PDFs kept working.

## Action items

1. Make the alias swap wait for bulk completion and verify document counts. (Done in the
   reindex fix PR.)
2. Alert when ES document count differs from Postgres by more than 0.5%. (Done.)
3. **Evaluate whether we need Elasticsearch at all.** Keeping a second datastore in sync is
   the root of this whole class of incident. Owner: @priya-natarajan.
"""

PDF_RUNBOOK = """
# Runbook: PDF rendering

PDFs are rendered by the `worker-pdf` Celery queue using **Playwright with headless
Chromium**. We moved off wkhtmltopdf in January 2026 (it is unmaintained, couldn't render
CSS grid photo layouts, and its bundled Qt WebKit has unpatched CVEs).

## How it works

1. `POST /v1/reports/{id}/pdf` enqueues `render_report_pdf`.
2. The worker renders `/print/reports/{id}` from the web app with a signed one-time token.
3. A warm pool of 3 Chromium browsers per task avoids the ~6 s cold start.
4. The PDF goes to S3; the client is notified over the report's event stream.

## Common problems

- **Backlog growing**: check `pdf_queue_depth`. Scale `worker-pdf` (each task 2 vCPU / 4 GB).
- **Blank photos**: S3 presigned URLs expired before render; they're valid for 10 minutes.
- **Out of memory**: a report with 500+ photos. The renderer pages photos in batches of 50.
"""

BILLING = """
# Billing

Stripe Billing. Decided in July 2025 (see the pricing issue in this repo).

- **Per seat**: each inspector seat is a subscription item (`price_seat_monthly`).
- Each seat includes **40 reports per month**; overage is metered per report.
- Annual plans get two months free.
- Enterprise plans (from 2026) add SSO/SCIM, priced per firm.

Stripe webhooks are handled by `worker-default` and are idempotent on the event id.
Stripe Tax collects US sales tax and UK VAT.
"""

DOCS = [
    Doc("README.md", README, key="readme"),
    Doc("docs/architecture.md", ARCHITECTURE, key="architecture"),
    Doc("docs/postmortems/2025-12-04-search-reindex.md", POSTMORTEM, key="postmortem"),
    Doc("docs/runbooks/pdf-rendering.md", PDF_RUNBOOK, key="pdf-runbook"),
    Doc("docs/billing.md", BILLING, key="billing-doc"),
]

ITEMS: list[PR | Issue] = [
    PR(
        "pr-bootstrap",
        "Bootstrap FastAPI service",
        "tomas-rivera",
        "2025-03-17 10:00",
        """
Skeleton per ADR-0002: FastAPI app, SQLAlchemy 2 async engine, Alembic, pytest with a
throwaway Postgres, ruff. `/healthz` and nothing else yet.
""",
        merged="2025-03-18 11:00",
        labels=["chore"],
        reviews=[approve("maya-chen", "2025-03-18 10:30", "LGTM, ship it.")],
    ),
    PR(
        "pr-heroku-deploy",
        "Deploy to Heroku: Procfile, release-phase migrations",
        "tomas-rivera",
        "2025-03-25 09:00",
        """
Per ADR-0003. `web`, `worker` and `release` (runs `alembic upgrade head`) process types.
Review apps enabled for PRs.
""",
        merged="2025-03-26 10:00",
        labels=["infra"],
        reviews=[approve("maya-chen", "2025-03-26 09:30")],
    ),
    PR(
        "pr-auth0-jwt",
        "Verify Auth0 JWTs on every request",
        "jonah-blake",
        "2025-04-10 10:00",
        """
Implements ADR-0004 on the API side: an auth dependency that verifies RS256 access tokens
from Auth0 against the tenant JWKS, checks `aud` and `iss`, and maps `sub` to our `users`
table (creating the row on first login).
""",
        merged="2025-04-15 12:00",
        labels=["security", "auth"],
        reviews=[
            comment_review(
                "tomas-rivera",
                "2025-04-11 09:30",
                "Looks good overall, two things inline.",
                comments=[
                    rc(
                        "larkspur/auth.py",
                        31,
                        "We fetch the JWKS on every request here. Cache it (with a TTL and a "
                        "refetch on unknown `kid`), otherwise we'll hit Auth0's rate limit.",
                        hunk="@@ -25,6 +25,12 @@ async def current_user(token: str = Depends(bearer)):\n+    jwks = httpx.get(settings.auth0_jwks_url).json()\n+    key = find_key(jwks, header['kid'])",
                        key="auth0-jwks-cache",
                    ),
                    rc(
                        "larkspur/auth.py",
                        48,
                        "Creating users on first login inside the auth dependency means a GET "
                        "can write. Fine for now, but let's make it an upsert so concurrent "
                        "first requests don't race.",
                    ),
                ],
            ),
            comment_review(
                "jonah-blake",
                "2025-04-14 10:00",
                "",
                comments=[
                    rc(
                        "larkspur/auth.py",
                        31,
                        "Done: cached for 10 minutes, refetched once on unknown kid.",
                        reply_to="auth0-jwks-cache",
                    )
                ],
            ),
            approve("tomas-rivera", "2025-04-15 11:00"),
        ],
    ),
    PR(
        "pr-es-indexer",
        "Index reports into Elasticsearch",
        "jonah-blake",
        "2025-04-28 10:00",
        """
Implements ADR-0005.

- `reports` index with an alias, so we can reindex without downtime
- Celery task `index_report(report_id)` called after every report save
- search endpoint `GET /search?q=` queries ES with a `bool` filter on `team_id` for
  permissions
- address field uses an edge-ngram analyzer + fuzziness AUTO
""",
        merged="2025-05-06 15:00",
        labels=["search"],
        reviews=[
            request_changes(
                "tomas-rivera",
                "2025-04-29 11:00",
                "The permission filter here only checks `team_id`. In Postgres we also hide "
                "reports shared via client links and drafts from other inspectors. We now have "
                "two implementations of who-can-see-what and they will drift. At minimum, add "
                "a test that runs the same cases against both.",
                key="es-indexer-tomas-permissions",
                comments=[
                    rc(
                        "larkspur/search/indexer.py",
                        57,
                        "If the task fails after the Postgres commit, the index silently misses "
                        "the report. Can we index by (id, version) and have a periodic job "
                        "catch up anything with a stale version?",
                        key="es-indexer-version",
                    )
                ],
            ),
            approve(
                "tomas-rivera",
                "2025-05-05 10:00",
                "Parity test + catch-up job added. Approving.",
            ),
            approve("maya-chen", "2025-05-06 14:00"),
        ],
    ),
    Issue(
        "issue-ci-flaky",
        "Flaky test: test_report_autosave_conflict",
        "tomas-rivera",
        "2025-05-12 09:00",
        "Fails ~1 in 10 runs on CI with a deadlock. Probably the two sessions in the test.",
        closed="2025-05-13 10:00",
        state_reason="completed",
        labels=["bug", "ci"],
        comments=[c("tomas-rivera", "2025-05-13 10:00", "Fixed by ordering the row locks.")],
    ),
    PR(
        "pr-celery",
        "Add Celery worker with Redis broker",
        "tomas-rivera",
        "2025-05-14 10:00",
        """
We need background jobs for ES indexing, emails and soon PDF rendering. This adds a
Celery app with Redis as broker and result backend, a `worker` Procfile entry and beat for
periodic jobs (the ES catch-up job).

Why Celery over RQ or Dramatiq: we need retries with backoff, beat scheduling and routing
to separate queues (PDFs will be heavy). RQ has no built-in scheduling; Dramatiq is nice
but nobody here has run it in production.
""",
        merged="2025-05-20 11:00",
        labels=["infra"],
        reviews=[
            approve(
                "maya-chen",
                "2025-05-19 16:00",
                "Approved. Celery is the boring choice and we know its failure modes. Keep PDF "
                "rendering on its own queue from day one.",
                key="celery-maya-approve",
            ),
        ],
    ),
    PR(
        "pr-sentry",
        "Add Sentry error tracking",
        "tomas-rivera",
        "2025-05-27 10:00",
        "Sentry SDK for FastAPI and Celery. DSN from env; 10% trace sampling.",
        merged="2025-05-27 15:00",
        labels=["chore"],
        reviews=[approve("jonah-blake", "2025-05-27 14:00")],
    ),
    PR(
        "pr-wkhtmltopdf",
        "Render inspection reports to PDF with wkhtmltopdf",
        "jonah-blake",
        "2025-06-03 10:00",
        """
Inspectors send clients a PDF report. This renders the report HTML template with Jinja and
converts it with wkhtmltopdf (via pdfkit) in a Celery task on the `pdf` queue.

Why wkhtmltopdf: one static binary, fast (about 1.5 s for a 30-page report), works on
Heroku with the apt buildpack. Headless Chrome would be more faithful but needs ~400 MB of
dependencies and more memory than our dynos have.
""",
        merged="2025-06-10 12:00",
        labels=["pdf"],
        reviews=[
            approve(
                "tomas-rivera",
                "2025-06-09 10:00",
                "Approve. Agree Chromium is too heavy for 1 GB dynos right now.",
                key="wkhtml-tomas-approve",
            ),
        ],
        comments=[
            c(
                "sam-whitfield",
                "2025-06-04 11:00",
                "Heads up: wkhtmltopdf uses an old Qt WebKit, so no flexbox or grid. I'll build "
                "the report template with tables. Photo grids will be a bit clunky.",
                key="wkhtml-sam-css",
            ),
        ],
    ),
    PR(
        "pr-v1-prefix",
        "Move all routes under /v1 (ADR-0007)",
        "tomas-rivera",
        "2025-06-18 10:00",
        "Mechanical change: every router mounted under `/v1`. Old paths redirect (308) for 30 days.",
        merged="2025-06-20 10:00",
        labels=["api"],
        reviews=[
            approve("sam-whitfield", "2025-06-19 15:00", "Web app updated in the matching PR.")
        ],
    ),
    Issue(
        "issue-sentry-noise",
        "Sentry: ignore ClientDisconnect errors",
        "tomas-rivera",
        "2025-06-25 10:00",
        "Mobile clients drop connections all the time. These aren't actionable.",
        closed="2025-06-26 10:00",
        state_reason="completed",
        labels=["chore"],
    ),
    Issue(
        "issue-pricing",
        "Billing model: per seat or per report?",
        "ines-duarte",
        "2025-07-01 09:00",
        """
We need to decide pricing before building billing. Options from the pilot interviews:

1. **Per seat** (per inspector per month). Predictable for firms; what competitors do.
2. **Per report**. Fair for part-time inspectors, but revenue swings with the season
   (inspections drop ~40% in winter).
3. Per seat with an included report allowance and overage.

I'm leaning to 3. Thoughts on what's easiest to build with Stripe?
""",
        closed="2025-07-08 17:00",
        state_reason="completed",
        labels=["billing", "product"],
        comments=[
            c(
                "tomas-rivera",
                "2025-07-01 14:00",
                "All three are easy in Stripe Billing. Option 3 is a licensed seat price plus a "
                "metered price for overage. Metered usage means we report usage on every "
                "finalized report, which is one Celery task.",
            ),
            c(
                "maya-chen",
                "2025-07-02 10:00",
                "Per report alone worries me: seasonal revenue makes hiring hard to plan. +1 for "
                "seats with an allowance.",
            ),
            c(
                "ines-duarte",
                "2025-07-08 16:50",
                "Decision: **per seat, 40 reports/month included per seat, overage billed per "
                "report**. Annual plans get two months free. Tomás, go ahead with Stripe "
                "Billing.",
                key="pricing-ines-decision",
            ),
        ],
    ),
    PR(
        "pr-stripe",
        "Stripe Billing: per-seat subscriptions with metered report overage",
        "tomas-rivera",
        "2025-07-09 10:00",
        """
Implements the pricing decided in {#issue-pricing}:

- Stripe Checkout for sign-up, Customer Portal for card changes
- seat quantity synced when inspectors are added/removed
- each finalized report reports usage; the first 40 per seat per month are free
- webhooks (`invoice.paid`, `customer.subscription.updated`) handled idempotently on event id

We chose Stripe over Paddle/Chargebee because we only sell in the US and UK for now and
Stripe Billing covers metered + licensed prices natively.
""",
        merged="2025-07-15 14:00",
        labels=["billing"],
        reviews=[
            approve(
                "maya-chen",
                "2025-07-15 11:00",
                "Approved. Nice that webhooks are idempotent from day one.",
                key="stripe-maya-approve",
            ),
        ],
        comments=[
            c(
                "ines-duarte",
                "2025-07-10 09:00",
                "Can the overage show as a separate line on the invoice? Firms will ask.",
            ),
            c(
                "tomas-rivera",
                "2025-07-10 11:00",
                "Yes, metered prices get their own invoice line.",
            ),
        ],
    ),
    PR(
        "pr-dependabot-fastapi",
        "Bump fastapi from 0.115.12 to 0.116.1",
        "dependabot[bot]",
        "2025-07-21 06:00",
        """
Bumps [fastapi](https://github.com/fastapi/fastapi) from 0.115.12 to 0.116.1.

Dependabot will resolve any conflicts with this PR as long as you don't alter it yourself.
""",
        merged="2025-07-22 09:00",
        labels=["dependencies"],
        branch="dependabot/pip/fastapi-0.116.1",
        reviews=[approve("tomas-rivera", "2025-07-22 08:50")],
        comments=[
            c(
                "github-actions[bot]",
                "2025-07-21 06:12",
                "✅ CI passed: 214 tests, coverage 81.2%.",
            ),
        ],
    ),
    Issue(
        "issue-stripe-tax",
        "Collect US sales tax and UK VAT",
        "ines-duarte",
        "2025-08-04 10:00",
        "Our accountant says we need to collect sales tax in a few states and VAT in the UK. Can Stripe do this?",
        closed="2025-08-12 10:00",
        state_reason="completed",
        labels=["billing"],
        comments=[
            c(
                "tomas-rivera",
                "2025-08-04 15:00",
                "Stripe Tax does it: 0.5% per transaction where we're registered. Turning it on "
                "is a flag on the Checkout session plus collecting addresses.",
            ),
            c("ines-duarte", "2025-08-05 09:00", "0.5% is fine. Please enable it."),
            c("tomas-rivera", "2025-08-12 10:00", "Enabled in production."),
        ],
    ),
    Issue(
        "issue-es-mapping",
        "Elasticsearch mapping explosion from custom defect fields",
        "priya-natarajan",
        "2025-08-11 10:00",
        """
Firms can define custom checklist fields, and we index them as dynamic fields. The
`reports` index now has 9,400 fields and hit `index.mapping.total_fields.limit`; new
reports with new custom fields fail to index (silently, the task just logs).
""",
        closed="2025-08-20 10:00",
        state_reason="completed",
        labels=["bug", "search"],
        comments=[
            c(
                "jonah-blake",
                "2025-08-11 14:00",
                "My bad, dynamic mapping was the quick option. We should flatten custom fields "
                "into a single `custom_text` field; nobody searches by a specific custom field.",
            ),
            c(
                "priya-natarajan",
                "2025-08-12 09:00",
                "Agreed. That also means a full reindex. Doing it in {#pr-flatten-fields}.",
            ),
            c(
                "tomas-rivera",
                "2025-08-12 10:00",
                "This is the second time a search problem was silent (see the missed-index "
                "issue in May). Can the indexer alert on failures?",
            ),
        ],
    ),
    PR(
        "pr-flatten-fields",
        "Flatten custom fields into custom_text before indexing",
        "priya-natarajan",
        "2025-08-14 10:00",
        """
Fixes {#issue-es-mapping}. Custom fields are concatenated into one `custom_text` field,
dynamic mapping is set to `strict`, and indexing failures now raise (so Sentry sees them).
Requires a full reindex, which I'll run on the weekend.
""",
        merged="2025-08-19 10:00",
        labels=["search"],
        reviews=[
            approve(
                "jonah-blake",
                "2025-08-18 15:00",
                "Approve. Reindex took 2h on staging; production will be ~6h.",
            ),
        ],
    ),
    PR(
        "pr-request-id",
        "Add request id middleware",
        "tomas-rivera",
        "2025-08-26 10:00",
        "Propagates `X-Request-Id` to logs, Sentry and Celery tasks.",
        merged="2025-08-26 14:00",
        labels=["chore"],
        reviews=[approve("priya-natarajan", "2025-08-26 13:00")],
    ),
    Issue(
        "issue-r14",
        "Heroku R14/R15 memory errors in the PDF worker",
        "kofi-mensah",
        "2025-09-08 10:00",
        """
Since big firms started uploading 300+ photos per report, the `pdf` worker dynos hit
R14 (memory quota exceeded) daily and R15 (killed) a few times a week. wkhtmltopdf loads
every image at full resolution.
""",
        closed="2025-09-12 10:00",
        state_reason="completed",
        labels=["bug", "infra", "pdf"],
        comments=[
            c(
                "tomas-rivera",
                "2025-09-08 13:00",
                "Short term: move the pdf worker to Performance-M (2.5 GB). That's about +$500 "
                "a month. Long term this is one more reason ADR-0003's exit criteria are close.",
                key="r14-tomas",
            ),
            c(
                "kofi-mensah",
                "2025-09-09 09:00",
                "Doing the Performance-M move as a stopgap. I'll write up an AWS cost comparison "
                "next month.",
            ),
        ],
    ),
    PR(
        "pr-perf-dynos",
        "Move pdf worker to Performance-M dynos",
        "kofi-mensah",
        "2025-09-10 10:00",
        "Stopgap for {#issue-r14}. Adds ~$500/month to the Heroku bill.",
        merged="2025-09-11 10:00",
        labels=["infra"],
        reviews=[approve("tomas-rivera", "2025-09-11 09:00")],
    ),
    Issue(
        "issue-es-permissions",
        "Search shows reports from a team the inspector left",
        "priya-natarajan",
        "2025-09-30 10:00",
        """
Support ticket: an inspector who moved from one branch office (team) to another still sees
the old team's reports in **search**, but not in the reports list. The ES documents carry
`team_id` at index time, and we don't reindex a user's visible reports when their team
changes.
""",
        closed="2025-10-03 10:00",
        state_reason="completed",
        labels=["bug", "search", "security"],
        comments=[
            c(
                "tomas-rivera",
                "2025-09-30 12:00",
                "This is exactly the drift I worried about in the indexer review: two "
                "permission models. Fix for now: filter ES hits through the Postgres permission "
                "query before returning them.",
                key="es-permissions-tomas",
            ),
            c(
                "priya-natarajan",
                "2025-10-01 10:00",
                "Post-filtering in Postgres means pages can come back short, but it's correct. "
                "Shipping that, plus a parity test for team moves.",
            ),
        ],
    ),
    PR(
        "pr-jonah-handoff",
        "Handoff notes: search indexing and PDF pipeline",
        "jonah-blake",
        "2025-10-06 10:00",
        """
My last week, so writing down what's in my head.

**Search (Elasticsearch)**
- Index alias `reports` → `reports_vN`. Full reindex = new index, bulk load, swap alias.
- The catch-up job (`reindex_stale`) runs every 10 min via beat.
- Things I'd watch: reindex needs disk headroom (the 2-node cluster has ~35% free), and
  the permission filter must stay in sync with Postgres (see the team-move bug).
- Honest take: at the volume we actually have (~1.4M reports) Postgres FTS might have been
  enough. I'd still keep ES if the 10M projection holds.

**PDFs (wkhtmltopdf)**
- No flexbox/grid support, so templates use tables.
- Memory scales with photo count; see the R14 issue.

Priya owns search from here, Tomás owns PDFs.
""",
        merged="2025-10-08 10:00",
        labels=["docs"],
        reviews=[approve("priya-natarajan", "2025-10-07 15:00", "Thank you Jonah, this is gold.")],
        comments=[
            c("maya-chen", "2025-10-07 16:00", "Thanks for everything Jonah 💐"),
        ],
    ),
    PR(
        "pr-uuidv7",
        "UUIDv7 primary keys for findings and photo uploads (ADR-0009)",
        "priya-natarajan",
        "2025-10-07 10:00",
        "New `findings` and `photo_uploads` tables use UUIDv7 generated on the client. Server validates the version nibble.",
        merged="2025-10-14 10:00",
        labels=["database"],
        reviews=[approve("tomas-rivera", "2025-10-13 15:00")],
    ),
    PR(
        "pr-dependabot-es",
        "Bump elasticsearch from 8.15.1 to 8.16.0",
        "dependabot[bot]",
        "2025-10-13 06:00",
        "Bumps [elasticsearch](https://github.com/elastic/elasticsearch-py) from 8.15.1 to 8.16.0.",
        merged="2025-10-14 09:00",
        labels=["dependencies"],
        branch="dependabot/pip/elasticsearch-8.16.0",
        reviews=[approve("priya-natarajan", "2025-10-14 08:30")],
        comments=[
            c(
                "codecov[bot]",
                "2025-10-13 06:20",
                "## Codecov Report\nAll modified and coverable lines are covered by tests ✅",
            ),
        ],
    ),
    Issue(
        "issue-heroku-pg-conn",
        "Heroku Postgres connection limit (120) reached at peak",
        "tomas-rivera",
        "2025-10-20 10:00",
        "Monday morning peak: `FATAL: too many connections`. Standard-0 caps at 120; web + workers + beat use ~110 at peak.",
        closed="2025-10-22 10:00",
        state_reason="completed",
        labels=["bug", "infra"],
        comments=[
            c(
                "kofi-mensah",
                "2025-10-20 12:00",
                "Added PgBouncer via the buildpack as a stopgap. Next Heroku Postgres tier is "
                "+$150/month. Adding this to the AWS comparison.",
            ),
        ],
    ),
    Issue(
        "issue-aws-plan",
        "Plan: migrate from Heroku to AWS",
        "kofi-mensah",
        "2025-10-27 10:00",
        """
ADR-0003's exit criteria are met (bill > $2k/month, enterprise customer needs private
networking). Proposed plan:

1. Terraform: VPC, ECS Fargate (api, worker-default, worker-pdf), RDS Postgres 16
   (multi-AZ), ElastiCache Redis, ALB, S3.
2. Logical replication from Heroku Postgres to RDS, then a 15-minute cutover window.
3. DNS cutover, keep Heroku running read-only for 48 h, then delete.

Cost estimate: ~$1,700/month vs $2,912 today. ADR to follow in the handbook.
""",
        closed="2025-12-03 18:00",
        state_reason="completed",
        labels=["infra"],
        assignees=["kofi-mensah"],
        comments=[
            c(
                "maya-chen",
                "2025-10-27 15:00",
                "Plan looks right. Please write the ADR so the reasoning is public, and make "
                "the PDF workers 4 GB on Fargate so we can finally consider Chromium.",
            ),
            c(
                "tomas-rivera",
                "2025-10-28 10:00",
                "Heroku Postgres doesn't allow logical replication out. We'll need a "
                "pg_dump/restore with a maintenance window. With 60 GB that's about 40 minutes.",
                key="aws-plan-tomas-dump",
            ),
            c(
                "ines-duarte",
                "2025-10-29 09:00",
                "A Sunday morning maintenance window is fine, I'll email customers a week ahead.",
            ),
            c("kofi-mensah", "2025-12-03 18:00", "Done: cutover went fine, Heroku app deleted."),
        ],
    ),
    Issue(
        "issue-api-docs",
        "OpenAPI docs page returns 500 in production",
        "sam-whitfield",
        "2025-11-05 10:00",
        "`/v1/docs` 500s. Works locally.",
        closed="2025-11-05 16:00",
        state_reason="completed",
        labels=["bug"],
        comments=[
            c(
                "tomas-rivera",
                "2025-11-05 13:00",
                "A Pydantic model with a recursive type broke schema generation. Fixing.",
            ),
            c("sam-whitfield", "2025-11-05 16:00", "Works now, thanks."),
        ],
    ),
    PR(
        "pr-terraform",
        "Terraform: ECS Fargate, RDS Postgres, ElastiCache, ALB",
        "kofi-mensah",
        "2025-11-19 10:00",
        """
Infrastructure for {#issue-aws-plan} / ADR-0010, in `infra/`.

- RDS Postgres 16, db.r6g.large, multi-AZ, 7-day PITR
- ECS services: api (2-6 tasks), worker-default (2), worker-pdf (2, 4 GB)
- ElastiCache Redis 7, single node + replica
- Secrets in SSM Parameter Store
""",
        merged="2025-11-27 10:00",
        labels=["infra"],
        reviews=[
            comment_review(
                "tomas-rivera",
                "2025-11-20 11:00",
                "",
                comments=[
                    rc(
                        "infra/rds.tf",
                        12,
                        "db.r6g.large is 16 GB. We're at 60 GB of data with a ~6 GB hot set, "
                        "so fine, but turn on Performance Insights so we can see if search "
                        "queries ever land here.",
                        key="terraform-rds-size",
                    ),
                    rc(
                        "infra/ecs.tf",
                        88,
                        "worker-pdf at 4 GB 👍 — that's what lets us drop wkhtmltopdf later.",
                    ),
                ],
            ),
            approve("tomas-rivera", "2025-11-26 10:00"),
            approve("maya-chen", "2025-11-26 17:00"),
        ],
    ),
    PR(
        "pr-cutover",
        "Cutover: switch DNS to the ALB and decommission Heroku",
        "kofi-mensah",
        "2025-12-01 10:00",
        """
Runbook for Sunday 2025-11-30 08:00 UTC is done; this PR removes the Procfile, Heroku
config and the review-app setup, and switches deploys to the GitHub Actions → ECS workflow.
""",
        merged="2025-12-03 10:00",
        labels=["infra"],
        reviews=[approve("maya-chen", "2025-12-02 15:00", "Great work Kofi. Go.")],
    ),
    Issue(
        "issue-search-missing",
        "INCIDENT: search missing a large share of reports",
        "priya-natarajan",
        "2025-12-04 09:20",
        """
Brightwell reports that searches return far fewer results than yesterday. ES doc count is
1.56M vs 1.90M reports in Postgres (~18% missing). Last night's full reindex looks stuck.
Investigating.
""",
        closed="2025-12-05 12:00",
        state_reason="completed",
        labels=["incident", "search"],
        assignees=["priya-natarajan"],
        comments=[
            c(
                "kofi-mensah",
                "2025-12-04 09:35",
                "Elastic Cloud shows the cluster over the disk high watermark; shards on the new "
                "index are unassigned. Scaling from 2 to 3 nodes now (cost goes up, we can "
                "scale down later).",
                key="search-missing-kofi",
            ),
            c(
                "priya-natarajan",
                "2025-12-04 10:10",
                "Root cause: the alias swap task ran as soon as the bulk task was *enqueued*, "
                "not when it finished. So the alias points at a half-built index. Restarting "
                "the bulk load.",
                key="search-missing-root-cause",
            ),
            c(
                "ines-duarte",
                "2025-12-04 10:30",
                "Three customers have emailed. I'm telling them reports are safe and search will "
                "be back today.",
            ),
            c(
                "priya-natarajan",
                "2025-12-04 11:25",
                "Bulk load complete, counts match. Search is back.",
            ),
            c(
                "tomas-rivera",
                "2025-12-04 12:00",
                "Third search incident caused by keeping ES in sync with Postgres (mapping limit "
                "in August, team-move permissions in September, now this). Let's put "
                "'do we need ES' on the postmortem action list.",
                key="search-missing-tomas",
            ),
        ],
    ),
    PR(
        "pr-fix-reindex",
        "Fix reindex: swap alias only after bulk load completes and counts match",
        "priya-natarajan",
        "2025-12-04 15:00",
        """
Fixes the root cause of {#issue-search-missing}. The reindex is now a Celery chain
(create → bulk → verify → swap). `verify` compares the ES count with Postgres and refuses
to swap if they differ by more than 0.5%. Also adds a CloudWatch alarm on the same check.
""",
        merged="2025-12-05 11:00",
        labels=["search", "bug"],
        reviews=[
            approve(
                "tomas-rivera",
                "2025-12-05 10:00",
                "Approve. The chain is right. Long term, see my comment on the incident.",
            ),
        ],
    ),
    PR(
        "pr-postmortem",
        "Postmortem: 2025-12-04 search reindex outage",
        "priya-natarajan",
        "2025-12-09 10:00",
        "Blameless postmortem for {#issue-search-missing}. Action item 3 is the big one: evaluate whether we need Elasticsearch.",
        merged="2025-12-11 10:00",
        labels=["docs", "incident"],
        reviews=[
            approve(
                "maya-chen",
                "2025-12-10 16:00",
                "Approved. I agree with action item 3; Priya owns it.",
            ),
            approve("kofi-mensah", "2025-12-11 09:00"),
        ],
    ),
    Issue(
        "issue-eval-pg-fts",
        "Evaluate replacing Elasticsearch with Postgres full-text search",
        "priya-natarajan",
        "2025-12-15 10:00",
        """
Postmortem action item 3. Questions to answer:

1. Latency: can Postgres FTS meet our 150 ms p95 budget on production-sized data?
2. Relevance: do top-10 results match ES closely enough on real queries?
3. Fuzzy address matching: how much do we lose?
4. Cost and ops: what do we save?

Plan: a spike on a production snapshot in January/February.
""",
        closed="2026-03-17 17:00",
        state_reason="completed",
        labels=["search"],
        assignees=["priya-natarajan"],
        comments=[
            c(
                "kofi-mensah",
                "2025-12-15 14:00",
                "Cost input: Elastic Cloud is now **$1,380/month** since we went to 3 nodes for "
                "the reindex. Scaling back to 2 nodes brings back the disk headroom problem.",
                key="eval-kofi-cost",
            ),
            c(
                "tomas-rivera",
                "2025-12-16 10:00",
                "Ops input: the sync job, the catch-up job, the parity test and the reindex "
                "chain all go away. Permission filtering lives only in SQL.",
            ),
            c(
                "maya-chen",
                "2025-12-17 11:00",
                "I'm open to it. I want real numbers, not vibes: p95 on a prod-sized snapshot "
                "and top-10 overlap on last month's real queries. If it's within budget we "
                "write the ADR.",
                key="eval-maya-criteria",
            ),
            c(
                "priya-natarajan",
                "2026-03-17 17:00",
                "Spike results in {#pr-fts-spike}, shadow-read results in {#pr-shadow-read}. "
                "ADR-0012 is merged. Closing.",
            ),
        ],
    ),
    Issue(
        "issue-wkhtml-broken",
        "PDF: photo grid layout broken in wkhtmltopdf",
        "lena-park",
        "2026-01-06 10:00",
        """
The new report design uses a CSS grid for photos (3 per row with captions). wkhtmltopdf
renders every photo full-width, one per page; a 200-photo report becomes 230 pages.
Workarounds with tables break captions across pages.
""",
        closed="2026-01-21 10:00",
        state_reason="completed",
        labels=["bug", "pdf"],
        comments=[
            c(
                "sam-whitfield",
                "2026-01-06 14:00",
                "wkhtmltopdf's WebKit is from ~2012, so no grid and no flexbox. The project is "
                "archived on GitHub; there won't be a fix.",
                key="wkhtml-archived",
            ),
            c(
                "ravi-kapoor",
                "2026-01-07 09:00",
                "Also a security concern: wkhtmltopdf bundles an old Qt WebKit with known CVEs, "
                "and we render HTML that includes customer-entered text. I'd want it gone.",
                key="wkhtml-ravi-cve",
            ),
            c(
                "tomas-rivera",
                "2026-01-07 11:00",
                "Now that worker-pdf has 4 GB on Fargate, Chromium is viable. I'll do Playwright.",
            ),
        ],
    ),
    PR(
        "pr-playwright",
        "Replace wkhtmltopdf with Playwright (headless Chromium) for PDFs",
        "tomas-rivera",
        "2026-01-13 10:00",
        """
Fixes {#issue-wkhtml-broken}.

Why:
- wkhtmltopdf is archived/unmaintained; no CSS grid or flexbox, so the new photo-grid
  design can't render
- its bundled Qt WebKit has unpatched CVEs (Ravi's point) and we render customer text
- on Fargate the PDF worker has 4 GB, so Chromium's memory is no longer a blocker
  (it was the reason we picked wkhtmltopdf on Heroku in June 2025)

How: the worker opens `/print/reports/{id}` in the web app with a one-time token and calls
`page.pdf()`. Rendering a 30-page report takes ~2.8 s warm (vs 1.5 s with wkhtmltopdf).
""",
        merged="2026-01-20 15:00",
        labels=["pdf"],
        reviews=[
            comment_review(
                "kofi-mensah",
                "2026-01-14 10:00",
                "",
                comments=[
                    rc(
                        "Dockerfile.worker",
                        7,
                        "This adds ~420 MB to the worker image. OK, but please use a separate "
                        "image for worker-pdf so the api and default worker stay small.",
                        key="playwright-image-size",
                    )
                ],
            ),
            comment_review(
                "tomas-rivera",
                "2026-01-14 15:00",
                "",
                comments=[
                    rc(
                        "Dockerfile.worker",
                        7,
                        "Split into `Dockerfile.pdf`; api image unchanged.",
                        reply_to="playwright-image-size",
                    )
                ],
            ),
            approve(
                "sam-whitfield",
                "2026-01-16 10:00",
                "Approve. Rendered the sample reports: photo grid and page breaks look exactly "
                "like the browser now.",
                key="playwright-sam-approve",
            ),
            approve(
                "maya-chen",
                "2026-01-20 14:00",
                "Approved. This reverses the wkhtmltopdf choice for good reasons: the "
                "constraint that drove it (1 GB Heroku dynos) is gone, and the security "
                "argument alone would justify it.",
                key="playwright-maya-approve",
            ),
        ],
    ),
    PR(
        "pr-remove-wkhtml",
        "Remove wkhtmltopdf and pdfkit",
        "tomas-rivera",
        "2026-01-26 10:00",
        "Cleanup after {#pr-playwright}. Old templates deleted.",
        merged="2026-01-27 10:00",
        labels=["pdf", "chore"],
        reviews=[approve("ravi-kapoor", "2026-01-27 09:00", "Happy to see this go.")],
    ),
    PR(
        "pr-fts-spike",
        "Spike: Postgres full-text search for reports (benchmark, do not merge)",
        "priya-natarajan",
        "2026-02-02 10:00",
        """
Spike for {#issue-eval-pg-fts}. Not for merging.

Setup: restored production snapshot (2.1M reports) on an RDS db.r6g.large clone. Generated
`tsvector` column: `setweight(address || client_name, 'A') || setweight(findings, 'B') ||
setweight(notes, 'C')`, GIN index (1.9 GB). Queries via `websearch_to_tsquery`, ranked by
`ts_rank_cd`, with the permission filter as a plain SQL join.

Results on 5,000 real queries from January:

| | Elasticsearch | Postgres FTS |
|---|---|---|
| p50 | 9 ms | 14 ms |
| p95 | 22 ms | 38 ms |
| p99 | 61 ms | 95 ms |
| top-10 overlap | — | 91% |

Budget is 150 ms p95, so both are fine. The 9% difference is mostly misspelled addresses,
where ES fuzziness wins.

Recommendation: move to Postgres FTS, accept weaker fuzzy matching for now, revisit with
pg_trgm if needed.
""",
        closed="2026-02-20 10:00",
        labels=["search", "spike"],
        reviews=[
            comment_review(
                "tomas-rivera",
                "2026-02-04 10:00",
                "Great write-up. One question: is the 38 ms with a cold or warm cache? The hot "
                "set is ~6 GB and the instance has 16 GB, so I'd expect warm in production.",
            ),
        ],
        comments=[
            c(
                "priya-natarajan",
                "2026-02-04 14:00",
                "Warm, after a 10-minute warm-up. Cold p95 was 120 ms.",
            ),
            c(
                "maya-chen",
                "2026-02-06 10:00",
                "This meets the bar I set in the evaluation issue. Please write the ADR and plan "
                "a shadow-read period before cutover.",
                key="spike-maya",
            ),
            c(
                "priya-natarajan",
                "2026-02-20 10:00",
                "Closing the spike; implementation in {#pr-tsvector}.",
            ),
        ],
    ),
    Issue(
        "issue-pdf-slow",
        "Some customers wait 40s+ for PDFs since the Chromium switch",
        "ines-duarte",
        "2026-02-09 10:00",
        "Two support tickets about slow PDFs this week. Is this the Playwright change?",
        closed="2026-02-18 10:00",
        state_reason="completed",
        labels=["bug", "pdf"],
        comments=[
            c(
                "tomas-rivera",
                "2026-02-09 13:00",
                "Yes, partly: each task launches a fresh Chromium (~6 s cold start), and the "
                "queue backs up at 9am. Fix: keep a warm browser pool per worker.",
            ),
            c("tomas-rivera", "2026-02-18 10:00", "Warm pool shipped; p95 back to ~4 s."),
        ],
    ),
    PR(
        "pr-typo-email",
        "Fix typo in password reset email",
        "lena-park",
        "2026-02-12 10:00",
        '"recieve" → "receive".',
        merged="2026-02-12 11:00",
        labels=["chore"],
        reviews=[approve("tomas-rivera", "2026-02-12 10:30")],
    ),
    PR(
        "pr-browser-pool",
        "Keep a warm Chromium pool in the PDF worker",
        "tomas-rivera",
        "2026-02-16 10:00",
        "Fixes {#issue-pdf-slow}. 3 browsers per task, recycled every 200 renders to cap memory.",
        merged="2026-02-17 10:00",
        labels=["pdf"],
        reviews=[approve("kofi-mensah", "2026-02-17 09:00")],
    ),
    PR(
        "pr-tsvector",
        "Add weighted tsvector column and GIN index on reports",
        "priya-natarajan",
        "2026-02-23 10:00",
        """
First step of the ES → Postgres FTS move (ADR in review in the handbook).

- `reports.search_tsv` generated column (address/client A, findings B, notes C)
- `reports_search_gin` GIN index, built `CONCURRENTLY`
- `search_reports()` query function with the permission join
Nothing reads it yet.
""",
        merged="2026-02-26 10:00",
        labels=["search", "database"],
        reviews=[
            comment_review(
                "tomas-rivera",
                "2026-02-24 10:00",
                "",
                comments=[
                    rc(
                        "migrations/versions/0042_report_search_tsv.py",
                        18,
                        "Generated column means every report update rewrites the tsvector. "
                        "Reports are updated a lot while drafting; did you measure write "
                        "overhead?",
                        key="tsvector-write-overhead",
                    )
                ],
            ),
            comment_review(
                "priya-natarajan",
                "2026-02-24 15:00",
                "",
                comments=[
                    rc(
                        "migrations/versions/0042_report_search_tsv.py",
                        18,
                        "Yes: +0.4 ms per update on the snapshot, and it removes the Celery "
                        "indexing task per save, so net it's less work.",
                        reply_to="tsvector-write-overhead",
                    )
                ],
            ),
            approve("tomas-rivera", "2026-02-25 10:00"),
        ],
    ),
    PR(
        "pr-shadow-read",
        "Shadow-read: run Postgres FTS alongside ES and log differences",
        "priya-natarajan",
        "2026-03-02 10:00",
        """
For 10% of search requests, also run `search_reports()` and log top-10 overlap and
latency. Users still get ES results.
""",
        merged="2026-03-04 10:00",
        labels=["search"],
        reviews=[approve("tomas-rivera", "2026-03-03 16:00")],
        comments=[
            c(
                "kofi-mensah",
                "2026-03-03 10:00",
                "RDS CPU headroom is fine for this; peak is 35%.",
            ),
            c(
                "priya-natarajan",
                "2026-03-16 10:00",
                "Shadow-read results after 12 days (61k queries): p95 41 ms, top-10 overlap "
                "93%. Most misses are misspelled addresses. No permission mismatches found "
                "(ES had 14 hits that Postgres correctly hid, all from the team-move class of "
                "bug).",
                key="shadow-results",
            ),
        ],
    ),
    PR(
        "pr-cutover-fts",
        "Serve search from Postgres FTS (behind search_backend flag)",
        "priya-natarajan",
        "2026-03-23 10:00",
        """
Implements ADR-0012. `/v1/search` reads from `search_reports()` when the `search_backend`
flag is `postgres` (default now). ES still indexed for one more week as a rollback path.
Response shape unchanged except `highlights` now come from `ts_headline`.
""",
        merged="2026-03-24 10:00",
        labels=["search"],
        reviews=[
            approve("tomas-rivera", "2026-03-23 15:00"),
            approve(
                "maya-chen",
                "2026-03-24 09:00",
                "Approved. Flip it on Tuesday morning UK time when we're all around.",
            ),
        ],
    ),
    Issue(
        "issue-saml",
        "Enterprise customers need SAML SSO and SCIM",
        "ines-duarte",
        "2026-03-30 10:00",
        """
Three firms in the enterprise pipeline (combined ~180 seats) require SAML SSO; two also
require SCIM provisioning. This blocks all three deals. What does it take?
""",
        closed="2026-05-20 13:00",
        state_reason="completed",
        labels=["auth", "enterprise"],
        comments=[
            c(
                "ravi-kapoor",
                "2026-03-31 10:00",
                "On Auth0 we'd need the Enterprise plan for SAML connections, and SCIM is a "
                "separate add-on. I asked for a quote: it's roughly 4x our current Auth0 bill "
                "before SCIM. Alternatives: WorkOS (per-connection pricing, SSO + SCIM in one "
                "API) or building SAML ourselves (no).",
                key="saml-ravi-quote",
            ),
            c(
                "tomas-rivera",
                "2026-04-01 09:00",
                "ADR-0004 warned about exactly this. Migrating users off Auth0 is the hard part; "
                "WorkOS supports importing bcrypt hashes, which Auth0 can export.",
            ),
            c(
                "maya-chen",
                "2026-04-02 10:00",
                "Ravi, please write an ADR comparing Auth0 Enterprise vs WorkOS with real "
                "numbers. Inês needs to sign off on the cost.",
            ),
            c(
                "ines-duarte",
                "2026-04-02 12:00",
                "WorkOS per-connection pricing maps onto our enterprise plan (we charge per "
                "firm). Auth0 Enterprise would eat most of the margin on the first three deals.",
                key="saml-ines-cost",
            ),
            c("ravi-kapoor", "2026-05-20 13:00", "ADR-0013 merged, implementation in progress."),
        ],
    ),
    PR(
        "pr-remove-es",
        "Remove Elasticsearch client, indexer and cluster config",
        "priya-natarajan",
        "2026-04-09 10:00",
        """
Two weeks on Postgres FTS with no rollback needed. This removes:

- `larkspur/search/es_*`, the indexing task, `reindex_stale` beat job and the reindex chain
- the ES/Postgres permission parity test
- the `elasticsearch` dependency and its settings

Kofi will delete the Elastic Cloud deployment after merge.
""",
        merged="2026-04-14 10:00",
        labels=["search", "chore"],
        reviews=[
            approve("tomas-rivera", "2026-04-13 10:00", "-2,140 lines. Beautiful."),
        ],
        comments=[
            c(
                "kofi-mensah",
                "2026-04-14 15:00",
                "Elastic Cloud deployment deleted. That's $1,380/month off the bill.",
                key="remove-es-kofi",
            ),
        ],
    ),
    PR(
        "pr-workos",
        "WorkOS: AuthKit sign-in, SSO and Directory Sync (SCIM)",
        "ravi-kapoor",
        "2026-05-21 10:00",
        """
Implements ADR-0013 on the API side.

- `/v1/auth/callback` exchanges the WorkOS code for a session; API verifies WorkOS JWTs
- SSO: organizations map to our `firms`; domain verification required
- Directory Sync webhooks create/deactivate inspectors (and update Stripe seat counts)
- Auth0 verification still accepted behind `AUTH_LEGACY_AUTH0=true` until migration
""",
        merged="2026-05-28 10:00",
        labels=["auth", "security"],
        reviews=[
            comment_review(
                "tomas-rivera",
                "2026-05-22 10:00",
                "",
                comments=[
                    rc(
                        "larkspur/auth/workos.py",
                        64,
                        "Directory Sync deactivation should also revoke active sessions, "
                        "otherwise a fired inspector keeps access until token expiry.",
                        key="workos-revoke",
                    ),
                    rc(
                        "larkspur/billing/seats.py",
                        22,
                        "SCIM-created users change seat counts: make sure we prorate rather "
                        "than invoice immediately, or firms will get a bill per new hire.",
                    ),
                ],
            ),
            comment_review(
                "ravi-kapoor",
                "2026-05-26 10:00",
                "",
                comments=[
                    rc(
                        "larkspur/auth/workos.py",
                        64,
                        "Good catch, sessions are revoked on `dsync.user.deleted` now.",
                        reply_to="workos-revoke",
                    )
                ],
            ),
            approve("tomas-rivera", "2026-05-27 10:00"),
            approve("maya-chen", "2026-05-27 16:00"),
        ],
    ),
    PR(
        "pr-auth0-migration",
        "Migrate Auth0 users to WorkOS (import password hashes)",
        "ravi-kapoor",
        "2026-06-01 10:00",
        """
One-off migration: export Auth0 users with bcrypt hashes (Auth0 support ticket), import
into WorkOS, map ids in `users.workos_id`. Users keep their passwords; Google logins
re-link by verified email. Dry run on staging: 4,212 users, 0 failures.
""",
        merged="2026-06-09 10:00",
        labels=["auth", "security"],
        reviews=[approve("tomas-rivera", "2026-06-08 10:00")],
        comments=[
            c(
                "ines-duarte",
                "2026-06-02 09:00",
                "I'll email all customers two days before: 'nothing changes for you, same "
                "password'. Please give me the date.",
            ),
            c("ravi-kapoor", "2026-06-02 10:00", "Tuesday 9 June, 07:00 UTC."),
        ],
    ),
    PR(
        "pr-remove-auth0",
        "Remove Auth0 SDK and legacy token verification",
        "ravi-kapoor",
        "2026-06-22 10:00",
        "Two weeks after migration, no Auth0 logins in the logs. Removing `AUTH_LEGACY_AUTH0` and the Auth0 client.",
        merged="2026-06-23 10:00",
        labels=["auth", "chore"],
        reviews=[approve("tomas-rivera", "2026-06-23 09:00")],
    ),
    PR(
        "pr-rate-limit",
        "Rate limit the public API with a Redis token bucket",
        "ravi-kapoor",
        "2026-06-25 10:00",
        """
Partners now call `/v1` with API keys, and one partner's sync job sent 40k requests in
10 minutes last week. This adds a per-API-key token bucket in Redis (100 req/min burst,
20 req/s sustained), returns 429 with `Retry-After`, and exempts the PWA's session traffic.
""",
        merged="2026-06-30 10:00",
        labels=["api", "security"],
        reviews=[
            comment_review(
                "tomas-rivera",
                "2026-06-26 10:00",
                "",
                comments=[
                    rc(
                        "larkspur/ratelimit.py",
                        12,
                        "Why in the app and not AWS WAF rate rules at the ALB?",
                        key="ratelimit-why-app",
                    )
                ],
            ),
            comment_review(
                "ravi-kapoor",
                "2026-06-26 14:00",
                "",
                comments=[
                    rc(
                        "larkspur/ratelimit.py",
                        12,
                        "WAF limits per IP, and partners run from shared cloud IPs. We need per "
                        "API key and per plan limits, which only the app knows. WAF stays as a "
                        "coarse DDoS backstop.",
                        reply_to="ratelimit-why-app",
                        key="ratelimit-ravi-reason",
                    )
                ],
            ),
            approve("tomas-rivera", "2026-06-29 10:00", "Makes sense. Approve."),
        ],
    ),
    Issue(
        "issue-webhook-retries",
        "Outgoing webhooks retry too aggressively",
        "tomas-rivera",
        "2026-07-13 10:00",
        "A partner's endpoint was down for an hour and we sent them 9k retries. We retry every 30 s forever.",
        closed="2026-07-20 10:00",
        state_reason="completed",
        labels=["bug", "api"],
        comments=[
            c(
                "priya-natarajan",
                "2026-07-13 12:00",
                "Exponential backoff capped at 1h, give up after 24h, and email the firm admin?",
            ),
            c(
                "ravi-kapoor",
                "2026-07-13 14:00",
                "+1, and sign webhook payloads while we're at it (HMAC header).",
            ),
            c("tomas-rivera", "2026-07-20 10:00", "Done in {#pr-webhook-backoff}."),
        ],
    ),
    PR(
        "pr-webhook-backoff",
        "Exponential backoff and HMAC signatures for outgoing webhooks",
        "tomas-rivera",
        "2026-07-15 10:00",
        "Fixes {#issue-webhook-retries}. Backoff 30s → 1h cap, stop after 24h, `Larkspur-Signature` HMAC-SHA256 header.",
        merged="2026-07-17 10:00",
        labels=["api"],
        reviews=[approve("ravi-kapoor", "2026-07-16 15:00")],
    ),
    Issue(
        "issue-fuzzy-address",
        "Misspelled addresses no longer find reports",
        "ines-duarte",
        "2026-07-28 10:00",
        """
Support has five tickets since spring: searching "1428 Elm Stret" or "Mt Pleasant Rd"
(stored as "Mount Pleasant Road") finds nothing. Customers say it used to work. Is this
from the search change?
""",
        closed="2026-09-10 12:00",
        state_reason="completed",
        labels=["search", "bug"],
        comments=[
            c(
                "priya-natarajan",
                "2026-07-28 13:00",
                "Yes. ES had fuzzy matching on addresses; Postgres FTS matches whole lexemes. "
                "We knew this (it was most of the 7-9% overlap gap in the spike) and ADR-0012 "
                "said we'd add pg_trgm if inspectors missed it. They do. I'll write ADR-0015.",
                key="fuzzy-priya",
            ),
            c(
                "tomas-rivera",
                "2026-07-29 10:00",
                "Please normalize abbreviations (Rd→Road, Mt→Mount) before trigram matching; "
                "trigrams alone won't bridge 'Mt' and 'Mount'.",
            ),
            c(
                "ines-duarte",
                "2026-07-29 11:00",
                "Thanks. Not asking to bring Elasticsearch back, just this.",
            ),
            c("priya-natarajan", "2026-09-10 12:00", "Shipped in {#pr-pg-trgm}."),
        ],
    ),
    PR(
        "pr-glacier",
        "S3 lifecycle: photo originals to Glacier Instant Retrieval after 90 days",
        "kofi-mensah",
        "2026-08-17 10:00",
        "Implements ADR-0014. Lifecycle rule on `photos/originals/`, 7-year expiration unless the object has the `legal-hold` tag.",
        merged="2026-08-18 10:00",
        labels=["infra", "compliance"],
        reviews=[
            approve("ravi-kapoor", "2026-08-18 09:00", "Expiry respects legal hold, approve.")
        ],
    ),
    Issue(
        "issue-lunch",
        "Lunch & learn: Terraform modules (Thursday)",
        "kofi-mensah",
        "2026-08-20 10:00",
        "I'll walk through how `infra/` is structured. Bring questions. Pizza on me 🍕",
        closed="2026-08-28 10:00",
        state_reason="completed",
        labels=["chatter"],
        comments=[
            c("lena-park", "2026-08-20 11:00", "Can we do pineapple this time"),
            c("tomas-rivera", "2026-08-20 11:05", "Absolutely not."),
            c("kofi-mensah", "2026-08-28 10:00", "Slides are in the drive. Thanks all!"),
        ],
    ),
    PR(
        "pr-pg-trgm",
        "Fuzzy address search with pg_trgm (ADR-0015)",
        "priya-natarajan",
        "2026-09-09 10:00",
        """
Fixes {#issue-fuzzy-address}.

- `CREATE EXTENSION pg_trgm`; `reports.address_norm` (lowercased, abbreviations expanded)
- `reports_address_trgm` GIN trigram index
- address-looking queries combine `ts_rank_cd` with `similarity(address_norm, q) > 0.3`

On the support-ticket test set, recall goes from 61% to 96% (ES was 95%).
""",
        merged="2026-09-10 10:00",
        labels=["search"],
        reviews=[
            approve("tomas-rivera", "2026-09-09 16:00", "Approve, abbreviation table looks good.")
        ],
    ),
    PR(
        "pr-dependabot-sqlalchemy",
        "Bump sqlalchemy from 2.0.41 to 2.0.43",
        "dependabot[bot]",
        "2026-09-14 06:00",
        "Bumps [sqlalchemy](https://github.com/sqlalchemy/sqlalchemy) from 2.0.41 to 2.0.43.",
        labels=["dependencies"],
        branch="dependabot/pip/sqlalchemy-2.0.43",
        comments=[
            c(
                "github-actions[bot]",
                "2026-09-14 06:15",
                "❌ CI failed: 2 tests failed in tests/test_search.py.",
            ),
        ],
    ),
    Issue(
        "issue-es-bill",
        "Elastic Cloud bill went from $640 to $1,380/month",
        "kofi-mensah",
        "2025-12-20 10:00",
        """
After scaling to 3 nodes during the 2025-12-04 incident, the Elastic Cloud deployment
costs $1,380/month. Scaling back to 2 nodes saves ~$740 but brings back the disk watermark
problem during reindexes. Keep 3 nodes?
""",
        closed="2026-04-14 16:00",
        state_reason="completed",
        labels=["infra", "search", "cost"],
        comments=[
            c(
                "maya-chen",
                "2025-12-20 14:00",
                "Keep 3 nodes until Priya's Postgres FTS evaluation is done. No point "
                "re-tuning a cluster we might delete.",
                key="es-bill-maya",
            ),
            c(
                "kofi-mensah",
                "2026-04-14 16:00",
                "Moot now: cluster deleted after {#pr-remove-es}.",
            ),
        ],
    ),
    Issue(
        "issue-timezone",
        "Inspection dates off by one day for Hawaii customers",
        "sam-whitfield",
        "2026-01-29 10:00",
        "We store `inspected_on` as a UTC timestamp and render the date in UTC.",
        closed="2026-02-02 10:00",
        state_reason="completed",
        labels=["bug"],
        comments=[
            c(
                "tomas-rivera",
                "2026-01-29 13:00",
                "It should be a `date` in the property's local time zone. Migration coming.",
            ),
            c("sam-whitfield", "2026-02-02 10:00", "Verified fixed."),
        ],
    ),
    Issue(
        "issue-rds-17",
        "Upgrade RDS to Postgres 17",
        "kofi-mensah",
        "2026-05-11 10:00",
        "PG 16 → 17 for faster vacuum on the big `photos` table and better JSON functions. Blue/green deployment, ~1 minute switchover.",
        closed="2026-06-14 10:00",
        state_reason="completed",
        labels=["infra", "database"],
        comments=[
            c(
                "priya-natarajan",
                "2026-05-12 10:00",
                "Please re-run the search benchmark on the green instance before switching; GIN behaviour changed a bit in 17.",
            ),
            c("kofi-mensah", "2026-06-14 10:00", "Upgraded. Search p95 unchanged (37 ms)."),
        ],
    ),
    Issue(
        "issue-csv-export",
        "Export findings to CSV",
        "ines-duarte",
        "2026-08-25 10:00",
        "Two firms want to analyze findings across all reports in Excel. A CSV export per date range would do.",
        labels=["feature"],
        comments=[
            c(
                "tomas-rivera",
                "2026-08-26 10:00",
                "Doable as a Celery job that emails a link. Not this sprint.",
            )
        ],
    ),
    Issue(
        "issue-pg-queue",
        "Proposal: move background jobs from Celery/Redis to a Postgres-backed queue?",
        "tomas-rivera",
        "2026-09-16 10:00",
        """
Now that search lives in Postgres, Redis is only a Celery broker plus rate limiting.
Options: keep Celery + Redis, or move jobs to a Postgres queue (procrastinate, or plain
`SKIP LOCKED`) for transactional enqueueing. Not urgent, opening for discussion.
""",
        labels=["discussion"],
        comments=[
            c(
                "kofi-mensah",
                "2026-09-16 14:00",
                "Redis also backs the rate limiter now, so it isn't going away either way.",
            ),
            c(
                "priya-natarajan",
                "2026-09-17 10:00",
                "Transactional enqueue would fix the 'committed but task never sent' class of "
                "bugs. But Celery works. I'd wait for a concrete problem.",
            ),
        ],
    ),
]

REPO = Repo(
    name="api",
    private=True,
    description="Larkspur backend: /v1 API, workers and infrastructure",
    created="2025-03-16 12:00",
    items=ITEMS,
    docs=DOCS,
)
