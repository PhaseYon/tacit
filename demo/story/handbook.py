"""larkspur/handbook: the public engineering handbook and ADRs."""

from .model import PR, Doc, Issue, Repo, approve, c, comment_review, rc, request_changes

H = "https://github.com/larkspur/handbook/blob/main/docs/adr"

# --- ADRs as they read on main today -------------------------------------------

ADR_0001 = """
# ADR-0001: Record architecture decisions

- Status: Accepted
- Date: 2025-03-10
- Deciders: @maya-chen

## Context

We are three engineers today and will be more. Decisions made in a call or a DM get lost,
and new people re-litigate them because they can't find the reasoning.

## Decision

We record every significant architecture decision as an ADR in `docs/adr/`, numbered
in order, using `docs/adr/template.md`. "Significant" means: hard to reverse, affects more
than one service, or adds a vendor or datastore.

- An ADR is proposed in a pull request against this repo.
- It needs approval from the CTO (@maya-chen) and at least one engineer who will own the
  change. See `docs/engineering/code-review.md`.
- ADRs are never deleted. When a decision changes we write a new ADR, set the old one's
  status to `Superseded by ADR-NNNN`, and link both ways.

## Consequences

The handbook is public, so ADRs must not contain customer names, credentials or
security details. Those go in the private `larkspur/api` repo.
"""

ADR_0002 = """
# ADR-0002: Python and FastAPI for the backend, Postgres as the system of record

- Status: Accepted
- Date: 2025-03-14
- Deciders: @maya-chen, @tomas-rivera, @jonah-blake

## Context

Larkspur lets home and property inspectors capture inspections on a phone, generate the
report and share it with their clients. We need an HTTP API for the web app and the
offline PWA, background jobs (PDF rendering, emails) and a relational store.

## Decision

- **Python 3.12 + FastAPI** for the API. Everyone on the team knows Python, FastAPI gives
  us OpenAPI for free, and async I/O suits a mostly I/O-bound API.
- **PostgreSQL** is the system of record for all business data.
- SQLAlchemy 2 + Alembic for data access and migrations.

We considered Node/NestJS (one language with the frontend) and Go. Hiring and the team's
experience decided it.

## Consequences

CPU-heavy work (PDF rendering, image processing) must run in background workers, not in
request handlers.
"""

ADR_0003 = f"""
# ADR-0003: Host on Heroku

- Status: Superseded by [ADR-0010]({H}/0010-aws-ecs-fargate-hosting.md) (2025-11-18)
- Date: 2025-03-24
- Deciders: @tomas-rivera, @maya-chen

## Context

We have no platform engineer. We want deploys to be `git push` and we want managed
Postgres and Redis with backups we don't have to think about.

## Decision

Run the API, the worker and the web app on Heroku (Standard-2X dynos), with Heroku
Postgres (Standard-0) and Heroku Data for Redis.

## Consequences

- Fast to set up; pipelines and review apps out of the box.
- Cost grows linearly with dynos. We'll revisit when the bill passes about $2k/month or an
  enterprise customer needs private networking.
- No VPC control, limited memory per dyno (1 GB on Standard-2X).
"""

ADR_0004 = f"""
# ADR-0004: Use Auth0 for authentication

- Status: Superseded by [ADR-0013]({H}/0013-workos-for-sso-and-auth.md) (2026-05-20)
- Date: 2025-04-08
- Deciders: @jonah-blake, @maya-chen, @tomas-rivera

## Context

Inspectors sign in with email and password, some with Google. We don't want to build
password storage, resets, MFA and brute-force protection ourselves.

## Decision

Use Auth0 (Essentials plan) for sign-up, sign-in, password reset and MFA. The API verifies
Auth0-issued JWTs (RS256) against Auth0's JWKS.

## Consequences

- Pricing is per monthly active user; fine at our size.
- Enterprise SAML connections are only on the Professional/Enterprise plans. If we sell to
  larger inspection firms we will need to revisit this.
"""

ADR_0005 = f"""
# ADR-0005: Elasticsearch for report search

- Status: Superseded by [ADR-0012]({H}/0012-postgres-full-text-search.md) (2026-03-17)
- Date: 2025-04-22
- Deciders: @jonah-blake (proposed), @maya-chen (approved)

## Context

Inspectors search their past reports by address, client name and defect text
("galvanized pipe", "double tapped breaker"). We expect address search to need fuzzy
matching and relevance tuning, and we projected 10M+ reports within two years.

## Decision

Index reports into **Elasticsearch** (Elastic Cloud, managed) and serve all report search
from it. Postgres stays the source of truth; a Celery job indexes changes.

Alternatives considered:
- **Postgres full-text search**: one less datastore, but weaker fuzzy matching and
  relevance tuning, and we worried about load on the primary at 10M+ reports.
- **Algolia**: great relevance, but per-record pricing gets expensive at our projected
  volume, and reports hold sensitive property data.

## Consequences

- A second datastore to keep in sync with Postgres (indexing job, reindex procedure).
- Permission filtering must be re-implemented in ES queries.
- Elastic Cloud cost starts at roughly $95/month and grows with data.
"""

ADR_0006 = f"""
# ADR-0006: Redux Toolkit for frontend state

- Status: Superseded by [ADR-0011]({H}/0011-tanstack-query-for-server-state.md) (2026-02-10)
- Date: 2025-04-30
- Deciders: @sam-whitfield, @maya-chen

## Context

The web app edits large inspection reports with many sections, photos and comments.
We need predictable state updates and good devtools.

## Decision

Use Redux Toolkit (RTK) with one slice per domain (reports, clients, templates, session).
Server data is fetched with thunks and stored in slices.

## Consequences

- Predictable, well-known, great devtools.
- More boilerplate than lighter options. We accepted it for consistency.
"""

ADR_0007 = """
# ADR-0007: Version the public API in the URL path

- Status: Accepted
- Date: 2025-06-17
- Deciders: @tomas-rivera, @maya-chen, @sam-whitfield

## Context

Inspection firms want to integrate (pull finished reports into their CRMs). The PWA also
caches API responses offline, so old clients stay around for weeks.

## Decision

All routes live under `/v1/...`. Breaking changes get a new prefix (`/v2`). We rejected
header-based versioning (`Accept: application/vnd.larkspur.v1+json`) because service
worker caches key on the URL, and rejected GraphQL for now (see the closed GraphQL proposal
in this repo).

## Consequences

We keep `/v1` alive for at least 12 months after `/v2` ships.
"""

ADR_0008 = """
# ADR-0008: Offline-first PWA instead of native mobile apps

- Status: Accepted (re-confirmed 2026-04-28)
- Date: 2025-08-05
- Deciders: @lena-park (proposed), @maya-chen, @sam-whitfield; product input from @ines-duarte

## Context

Inspectors work in basements, crawl spaces and rural properties with no signal. They must
be able to capture an entire inspection offline (text, photos, checklists) and sync later.

## Decision

Build the mobile experience as an **offline-first Progressive Web App**: a service worker,
IndexedDB for drafts and a durable outbox for photo uploads, synced with background sync
where available. No native iOS/Android apps for now.

We considered React Native. It would give better background upload on iOS, but it means a
second codebase, app store releases and review delays, for a team of two frontend engineers.

## Consequences

- iOS Safari storage limits and missing Background Sync need workarounds (client-side photo
  compression, foreground sync on app open).
- We'll revisit native apps if a large customer requires MDM distribution or if offline
  photo loss becomes a real support problem.
- 2026-04-28: re-confirmed after a customer asked for a native app; see the closed issue
  in `larkspur/web`. Revisit at 50 enterprise seats.
"""

ADR_0009 = """
# ADR-0009: UUIDv7 primary keys for new tables

- Status: Accepted
- Date: 2025-10-02
- Deciders: @priya-natarajan (proposed), @tomas-rivera, @maya-chen

## Context

Offline PWA clients create reports, findings and photos before the server knows about them.
Sequential integer ids need a server round trip and leak business volume in URLs. Random
UUIDv4 keys fragment B-tree indexes on our largest tables.

## Decision

New tables use **UUIDv7** primary keys, generated on the client or server. Existing tables
keep their bigint keys; we won't migrate them.

## Consequences

Time-ordered keys keep inserts index-friendly. Ids can be created offline without
collisions.
"""

ADR_0010 = f"""
# ADR-0010: Move hosting from Heroku to AWS (ECS Fargate + RDS)

- Status: Accepted
- Date: 2025-11-18
- Supersedes: [ADR-0003]({H}/0003-heroku-hosting.md)
- Deciders: @kofi-mensah (proposed), @maya-chen, @tomas-rivera

## Context

ADR-0003 chose Heroku while we had no platform engineer. Since then:

1. **Cost.** The Heroku bill reached about $2,900/month in October 2025 (mostly
   Performance dynos for PDF rendering). The same workload on Fargate + RDS is estimated
   at about $1,700/month.
2. **Memory.** PDF workers hit R14/R15 memory errors on 1 GB dynos (see the R14 issue in
   `larkspur/api`); the next dyno size doubles the price.
3. **Private networking.** Our first enterprise prospect requires VPC peering for their
   SSO and data warehouse, which on Heroku means Private Spaces (about $1,000/month extra).
4. We now have a platform engineer (@kofi-mensah).

## Decision

Run the API, workers and web app on **AWS ECS Fargate** in `eu-west-1` and `us-east-1`,
with **RDS for PostgreSQL**, ElastiCache for Redis and S3 for photos. Infrastructure is
managed with Terraform in `larkspur/api/infra`.

## Consequences

- We own more operations: patching base images, alarms, Terraform state.
- Deploys go through GitHub Actions to ECS instead of `git push heroku`.
- Heroku was decommissioned on 2025-12-03.
"""

ADR_0011 = f"""
# ADR-0011: TanStack Query for server state, Zustand for client state

- Status: Accepted
- Date: 2026-02-10
- Supersedes: [ADR-0006]({H}/0006-redux-toolkit-frontend-state.md)
- Deciders: @lena-park (proposed), @sam-whitfield

## Context

ADR-0006 put all state in Redux Toolkit slices. A year in, about 80% of what lives in
Redux is a cache of server data: we hand-write loading/error flags, cache invalidation and
optimistic updates in every slice, and offline replay of thunks is fragile. Adding a new
screen takes a slice, thunks, selectors and tests.

## Decision

- **TanStack Query** owns server state (fetching, caching, invalidation, retries) and
  persists its cache to IndexedDB for offline use.
- **Zustand** holds the small amount of real client state (editor UI, the offline outbox
  status).
- Redux is removed once the last slice is migrated.

## Consequences

Less code per feature and one place for cache policy. We lose time-travel devtools, which
nobody used. Migration happens screen by screen.
"""

ADR_0012 = f"""
# ADR-0012: Replace Elasticsearch with Postgres full-text search

- Status: Accepted
- Date: 2026-03-17
- Supersedes: [ADR-0005]({H}/0005-elasticsearch-for-report-search.md)
- Deciders: @priya-natarajan (proposed), @maya-chen, @tomas-rivera

## Context

ADR-0005 chose Elasticsearch expecting 10M+ reports and heavy relevance tuning. A year
later:

- We have **2.1M reports**, growing about 90k/month, far below the projection.
- Keeping ES in sync with Postgres has caused incidents, the worst on 2025-12-04 when a
  reindex left about 18% of reports unsearchable for nine hours.
- Permission filtering is implemented twice (SQL and ES queries) and has drifted.
- Elastic Cloud costs about **$1,380/month** after we scaled the cluster for reindexing.
- We never used most of the relevance tuning we chose ES for.

A spike in February 2026 measured Postgres FTS (`tsvector` + GIN index, `websearch_to_tsquery`)
on a production-sized copy: p95 38 ms vs 22 ms on ES, well inside our 150 ms budget.

## Decision

Serve report search from **Postgres full-text search** on the primary database: a
generated `tsvector` column with weights (address and client name A, findings B, notes C),
a GIN index, and `ts_rank_cd` ordering. Remove Elasticsearch after a shadow-read period.

Alternatives considered: Meilisearch and Typesense (still a second datastore to sync) and
staying on ES with a better reindex procedure (keeps the cost and the duplicate permission
logic).

## Consequences

- One datastore, one permission model, no sync job.
- Fuzzy address matching is weaker than ES; we accept that and will add `pg_trgm` if
  inspectors complain (done in ADR-0015).
- If we pass about 20M reports or search load hurts the primary, we revisit (read replica
  first).
"""

ADR_0013 = f"""
# ADR-0013: Use WorkOS for authentication, SSO and SCIM

- Status: Accepted
- Date: 2026-05-20
- Supersedes: [ADR-0004]({H}/0004-auth0-for-authentication.md)
- Deciders: @ravi-kapoor (proposed), @maya-chen; pricing input from @ines-duarte

## Context

Larger inspection firms now require SAML SSO and SCIM user provisioning. On Auth0 that
needs the Enterprise plan; the quote we received was far above our budget, and SCIM was an
add-on on top. Our first three enterprise customers each use a different IdP (Okta, Entra ID,
Google Workspace).

## Decision

Move authentication to **WorkOS** (AuthKit for email/password and Google, SSO and Directory
Sync for SCIM). WorkOS charges per SSO connection, which matches how we sell enterprise
plans. Existing Auth0 users are migrated by importing password hashes, so nobody has to
reset their password.

## Consequences

- One integration covers SAML, OIDC and SCIM for every IdP.
- Auth0 is removed after migration.
- Per-connection pricing must be built into the enterprise plan price.
"""

ADR_0014 = """
# ADR-0014: Retention of inspection photos and reports

- Status: Accepted
- Date: 2026-08-11
- Deciders: @ravi-kapoor, @ines-duarte, @maya-chen

## Context

Inspection reports are legal records: several US states and UK surveying bodies expect
firms to keep them for 5 to 7 years. Photos are 85% of our storage cost and are rarely
opened after the first 90 days.

## Decision

- Keep reports and photos for **7 years** after the inspection date, then delete them
  unless the customer has enabled legal hold.
- Move photo **originals** to S3 Glacier Instant Retrieval after **90 days**; keep the
  compressed web versions in S3 Standard.
- Customers can export everything at any time; deletion after 7 years is announced 30 days
  ahead.

## Consequences

Storage cost for photos drops by about 60%. Opening an old original is slightly slower.
"""

ADR_0015 = f"""
# ADR-0015: Fuzzy address matching with pg_trgm

- Status: Accepted
- Date: 2026-09-08
- Amends: [ADR-0012]({H}/0012-postgres-full-text-search.md)
- Deciders: @priya-natarajan (proposed), @tomas-rivera

## Context

After moving search to Postgres FTS (ADR-0012), inspectors reported that misspelled or
abbreviated addresses ("1428 Elm Stret", "Mt Pleasant Rd") no longer match, which ES's fuzzy
queries handled.

## Decision

Add the `pg_trgm` extension and a trigram GIN index on the normalized address. Address
queries combine FTS rank with trigram similarity (`similarity() > 0.3`). Full-text search
stays the main engine; we are **not** bringing back Elasticsearch.

## Consequences

Slightly larger index (about 1.2 GB). Address search recall is back to the ES level in our
test set.
"""

TEMPLATE = """
# ADR-NNNN: Title

- Status: Proposed | Accepted | Superseded by ADR-NNNN
- Date: YYYY-MM-DD
- Deciders: @login, ...

## Context

What forces are at play? What problem are we solving?

## Decision

What did we decide, and which alternatives did we reject?

## Consequences

What becomes easier or harder?
"""

README = f"""
# Larkspur engineering handbook

How we build Larkspur, the inspection reporting app for home and property inspectors.
This repo is public on purpose: we think working in the open makes us better.

## Architecture decision records

| ADR | Title | Status |
|---|---|---|
| [0001]({H}/0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002]({H}/0002-python-fastapi-postgres.md) | Python, FastAPI and Postgres | Accepted |
| [0003]({H}/0003-heroku-hosting.md) | Host on Heroku | Superseded by 0010 |
| [0004]({H}/0004-auth0-for-authentication.md) | Auth0 for authentication | Superseded by 0013 |
| [0005]({H}/0005-elasticsearch-for-report-search.md) | Elasticsearch for report search | Superseded by 0012 |
| [0006]({H}/0006-redux-toolkit-frontend-state.md) | Redux Toolkit for frontend state | Superseded by 0011 |
| [0007]({H}/0007-url-path-api-versioning.md) | URL path API versioning | Accepted |
| [0008]({H}/0008-offline-first-pwa.md) | Offline-first PWA, no native apps | Accepted |
| [0009]({H}/0009-uuidv7-primary-keys.md) | UUIDv7 primary keys | Accepted |
| [0010]({H}/0010-aws-ecs-fargate-hosting.md) | AWS ECS Fargate + RDS | Accepted |
| [0011]({H}/0011-tanstack-query-for-server-state.md) | TanStack Query + Zustand | Accepted |
| [0012]({H}/0012-postgres-full-text-search.md) | Postgres full-text search | Accepted |
| [0013]({H}/0013-workos-for-sso-and-auth.md) | WorkOS for auth, SSO and SCIM | Accepted |
| [0014]({H}/0014-photo-and-report-retention.md) | Photo and report retention | Accepted |
| [0015]({H}/0015-pg-trgm-fuzzy-address-search.md) | Fuzzy address matching with pg_trgm | Accepted |

## Other docs

- [Code review](docs/engineering/code-review.md)
- [On-call](docs/engineering/oncall.md)
- [Tech radar](docs/engineering/tech-radar.md)
"""

CODE_REVIEW = """
# Code review

## Everyday PRs

- One approval from someone who knows the area. Authors merge their own PRs (squash).
- Keep PRs small. Over ~400 changed lines, split it or explain why not.
- "Nit:" means optional. Anything else, resolve or reply before merging.

## ADRs

- ADR PRs need an approval from the CTO (@maya-chen) **and** from an engineer who will own
  the change.
- Decisions about vendors that cost more than $500/month also need a 👍 from the CEO
  (@ines-duarte) on the PR.
- Leave an ADR PR open at least two working days so people can weigh in.

## Security-sensitive changes

Auth, permissions, billing and data retention changes need a review from @ravi-kapoor
(since January 2026; before that @tomas-rivera).
"""

ONCALL = """
# On-call

One engineer is on call each week (Monday 10:00 to Monday 10:00 UK time). Pages come from
CloudWatch alarms via PagerDuty.

## Runbooks

### Search is slow or returns nothing

Search runs on Postgres full-text search (ADR-0012); there is no separate search cluster
any more.

1. Check RDS CPU and the `report_search_p95` dashboard.
2. `EXPLAIN ANALYZE` the slow query from the API logs; make sure it uses
   `reports_search_gin` (and `reports_address_trgm` for address queries).
3. If the GIN index is bloated, `REINDEX INDEX CONCURRENTLY reports_search_gin`.

### PDF rendering backlog

PDFs are rendered by headless Chromium (Playwright) in the `pdf` Celery queue. See
`docs/runbooks/pdf-rendering.md` in `larkspur/api`.

### Login failures

Auth is WorkOS (ADR-0013). Check status.workos.com first, then the `auth_callback_errors`
metric.
"""

TECH_RADAR = """
# Tech radar

Last updated 2026-06-02.

## Adopt

- Python 3.12, FastAPI, SQLAlchemy 2, Alembic
- PostgreSQL (RDS), including full-text search and pg_trgm
- Celery + Redis for background jobs
- TanStack Query, Zustand, Next.js
- Playwright (headless Chromium) for PDF rendering
- WorkOS for auth, SSO and SCIM
- Terraform, ECS Fargate

## Trial

- OpenTelemetry tracing to Honeycomb

## Hold

- Redux Toolkit (being removed, ADR-0011)
- GraphQL (rejected for the public API, see ADR-0007)

## Retired

- Elasticsearch (replaced by Postgres FTS, ADR-0012; cluster deleted April 2026)
- Heroku (replaced by AWS, ADR-0010)
- Auth0 (replaced by WorkOS, ADR-0013)
- wkhtmltopdf (replaced by Playwright, January 2026)
"""

DOCS = [
    Doc("README.md", README, key="readme"),
    Doc("docs/adr/0001-record-architecture-decisions.md", ADR_0001, key="adr-0001"),
    Doc("docs/adr/0002-python-fastapi-postgres.md", ADR_0002, key="adr-0002"),
    Doc("docs/adr/0003-heroku-hosting.md", ADR_0003, key="adr-0003"),
    Doc("docs/adr/0004-auth0-for-authentication.md", ADR_0004, key="adr-0004"),
    Doc("docs/adr/0005-elasticsearch-for-report-search.md", ADR_0005, key="adr-0005"),
    Doc("docs/adr/0006-redux-toolkit-frontend-state.md", ADR_0006, key="adr-0006"),
    Doc("docs/adr/0007-url-path-api-versioning.md", ADR_0007, key="adr-0007"),
    Doc("docs/adr/0008-offline-first-pwa.md", ADR_0008, key="adr-0008"),
    Doc("docs/adr/0009-uuidv7-primary-keys.md", ADR_0009, key="adr-0009"),
    Doc("docs/adr/0010-aws-ecs-fargate-hosting.md", ADR_0010, key="adr-0010"),
    Doc("docs/adr/0011-tanstack-query-for-server-state.md", ADR_0011, key="adr-0011"),
    Doc("docs/adr/0012-postgres-full-text-search.md", ADR_0012, key="adr-0012"),
    Doc("docs/adr/0013-workos-for-sso-and-auth.md", ADR_0013, key="adr-0013"),
    Doc("docs/adr/0014-photo-and-report-retention.md", ADR_0014, key="adr-0014"),
    Doc("docs/adr/0015-pg-trgm-fuzzy-address-search.md", ADR_0015, key="adr-0015"),
    Doc("docs/adr/template.md", TEMPLATE, key="adr-template"),
    Doc("docs/engineering/code-review.md", CODE_REVIEW, key="code-review"),
    Doc("docs/engineering/oncall.md", ONCALL, key="oncall"),
    Doc("docs/engineering/tech-radar.md", TECH_RADAR, key="tech-radar"),
]

# --- PRs and issues ------------------------------------------------------------

ITEMS: list[PR | Issue] = [
    PR(
        "pr-adr-0001",
        "Add ADR process (ADR-0001) and template",
        "maya-chen",
        "2025-03-10 09:12",
        """
Starting the handbook. ADR-0001 says how we record decisions; the template is in
`docs/adr/template.md`.

Rule of thumb: if you'd have to explain it to a new hire in six months, write an ADR.
""",
        merged="2025-03-10 17:05",
        labels=["adr"],
        reviews=[approve("tomas-rivera", "2025-03-10 14:30")],
        comments=[
            c(
                "jonah-blake",
                "2025-03-10 11:02",
                "Love this. Can we also say that superseded ADRs stay in the repo? I've seen "
                "teams delete old ones and then nobody knows why something was tried before.",
            ),
            c(
                "maya-chen",
                "2025-03-10 11:40",
                "Good call, added: ADRs are never deleted, the old one gets "
                "`Superseded by ADR-NNNN`.",
            ),
        ],
    ),
    PR(
        "pr-adr-0002",
        "ADR-0002: Python + FastAPI backend, Postgres as system of record",
        "maya-chen",
        "2025-03-12 10:20",
        """
Writing down what we already agreed on the kickoff call: Python/FastAPI, Postgres,
SQLAlchemy + Alembic. Also lists why not Node/NestJS or Go.
""",
        merged="2025-03-14 16:10",
        labels=["adr"],
        reviews=[
            approve(
                "tomas-rivera",
                "2025-03-13 09:45",
                "Approving. One note for the record: FastAPI's async story is great for I/O but "
                "PDF rendering must never run in a request handler. Worth a line in "
                "Consequences.",
            ),
            approve("jonah-blake", "2025-03-14 15:30"),
        ],
    ),
    PR(
        "pr-adr-0003",
        "ADR-0003: Host on Heroku",
        "tomas-rivera",
        "2025-03-20 13:00",
        """
Proposal: Heroku for API, worker and web, plus Heroku Postgres and Redis.

We don't have anyone to run infrastructure and we need to ship the pilot in April. Heroku
pipelines + review apps get us there. I put the exit criteria in Consequences: bill over
~$2k/month, or a customer needing private networking.
""",
        merged="2025-03-24 10:15",
        labels=["adr", "infra"],
        reviews=[
            approve(
                "maya-chen",
                "2025-03-21 18:20",
                "Fine for now. I agree with the exit criteria, let's actually check them when "
                "we sign the first enterprise customer instead of drifting.",
                key="adr-0003-maya-approve",
            ),
        ],
        comments=[
            c(
                "jonah-blake",
                "2025-03-20 16:45",
                "Fly.io would be cheaper, but I agree Heroku is the boring choice and boring is "
                "what we want for the pilot.",
            )
        ],
    ),
    PR(
        "pr-adr-0004",
        "ADR-0004: Auth0 for authentication",
        "jonah-blake",
        "2025-04-03 10:30",
        """
We shouldn't build password storage, resets and MFA ourselves. Auth0 Essentials covers
email/password, Google login and MFA. The API verifies RS256 JWTs against the JWKS.

Looked at Cognito (cheaper, but the hosted UI is rough and customizing it is painful) and
Clerk (nice DX, very React-centric, less mature for our API-first setup).
""",
        merged="2025-04-08 12:00",
        labels=["adr", "security"],
        reviews=[
            comment_review(
                "tomas-rivera",
                "2025-04-04 09:10",
                "",
                comments=[
                    rc(
                        "docs/adr/0004-auth0-for-authentication.md",
                        24,
                        "Worth noting that SAML connections are only on Professional/Enterprise. "
                        "If we ever sell to big inspection firms this will bite us.",
                        key="adr-0004-saml-warning",
                    )
                ],
            ),
            approve("tomas-rivera", "2025-04-07 11:20"),
            approve("maya-chen", "2025-04-08 10:05", "Approved. Added the SAML caveat."),
        ],
    ),
    PR(
        "pr-adr-0005",
        "ADR-0005: Elasticsearch for report search",
        "jonah-blake",
        "2025-04-15 14:00",
        """
Search is the feature inspectors ask about most in pilot interviews: "find that report on
Maple Ave from last spring", "every report where we flagged polybutylene pipe".

Proposal: index reports into Elasticsearch on Elastic Cloud and serve search from there.

Why ES and not Postgres FTS:
- fuzzy address matching (typos, abbreviations) out of the box
- relevance tuning (boost address and client over notes)
- we expect 10M+ reports in two years; I don't want search load on the primary

Costs: about $95/month to start, plus an indexing job.
""",
        merged="2025-04-22 11:30",
        labels=["adr", "search"],
        reviews=[
            request_changes(
                "tomas-rivera",
                "2025-04-16 10:20",
                "I'm not convinced yet. This adds a second datastore we have to keep in sync, a "
                "reindex procedure, and a second copy of the permission rules. Postgres FTS "
                "with a GIN index handles a few million rows fine. Can we at least list Postgres "
                "FTS as an alternative with the reasons we rejected it?",
                key="adr-0005-tomas-concern",
            ),
            approve(
                "tomas-rivera",
                "2025-04-21 15:00",
                "Alternatives section addresses it. Not my first choice but I won't block.",
            ),
            approve(
                "maya-chen",
                "2025-04-22 10:45",
                "Going with ES. Search quality is a selling point for the pilot and Jonah has "
                "run ES before. Use Elastic Cloud so we don't operate it ourselves. If keeping "
                "it in sync turns into real ops work, we revisit.",
                key="adr-0005-maya-approve",
            ),
        ],
        comments=[
            c(
                "jonah-blake",
                "2025-04-17 09:30",
                "Added an Alternatives section. Short version: Postgres FTS has no good fuzzy "
                "matching (pg_trgm helps, but tuning it per field is manual), and I'd rather "
                "not put 10M+ reports of search load on the primary. Algolia: per-record "
                "pricing and sensitive property data.",
                key="adr-0005-jonah-reply",
            ),
        ],
    ),
    PR(
        "pr-adr-0006",
        "ADR-0006: Redux Toolkit for frontend state",
        "sam-whitfield",
        "2025-04-28 09:00",
        """
The report editor has deeply nested state (sections → items → findings → photos) and
we need undo and good devtools. Proposal: Redux Toolkit, one slice per domain.
""",
        merged="2025-04-30 15:20",
        labels=["adr", "frontend"],
        reviews=[approve("maya-chen", "2025-04-30 14:00")],
        comments=[
            c(
                "jonah-blake",
                "2025-04-28 13:15",
                "RTK Query would cut a lot of the thunk boilerplate for server data. Worth a look?",
            ),
            c(
                "sam-whitfield",
                "2025-04-28 15:40",
                "Looked at it. I'd rather keep one pattern for now (thunks + slices) while the "
                "team is small. Can revisit.",
            ),
        ],
    ),
    PR(
        "pr-code-review",
        "Add code review guidelines",
        "maya-chen",
        "2025-05-02 10:00",
        "How we review PRs and who approves ADRs. Mostly writing down what we already do.",
        merged="2025-05-05 09:30",
        labels=["docs"],
        reviews=[
            approve("tomas-rivera", "2025-05-02 16:00"),
            approve("sam-whitfield", "2025-05-05 09:00"),
        ],
    ),
    PR(
        "pr-adr-0007",
        "ADR-0007: URL path API versioning",
        "tomas-rivera",
        "2025-06-12 11:00",
        """
First integration partner wants to pull reports into their CRM, so we need a versioning
policy before we publish the API. Proposal: `/v1/...` in the path.
""",
        merged="2025-06-17 10:00",
        labels=["adr", "api"],
        reviews=[
            comment_review(
                "sam-whitfield",
                "2025-06-13 10:30",
                "+1 to path versioning. Header versioning would be painful for the PWA: the "
                "service worker cache keys on the URL, so two API versions would collide in "
                "the cache.",
                key="adr-0007-sam-cache",
            ),
            approve("maya-chen", "2025-06-16 17:00"),
            approve("sam-whitfield", "2025-06-17 09:10"),
        ],
    ),
    PR(
        "pr-typo-0005",
        "Fix typos in ADR-0005",
        "lena-park",
        "2025-07-01 08:40",
        "teh → the, Elasticsearh → Elasticsearch.",
        merged="2025-07-01 09:05",
        labels=["docs"],
        reviews=[approve("sam-whitfield", "2025-07-01 09:00")],
    ),
    PR(
        "pr-adr-0008",
        "ADR-0008: Offline-first PWA instead of native apps",
        "lena-park",
        "2025-07-29 10:00",
        """
After the field visits last week: inspectors lose signal in basements and crawl spaces
all the time, and two pilot users lost photos when the browser tab reloaded.

Proposal: make the mobile web app an offline-first PWA (service worker, IndexedDB drafts,
durable photo outbox) rather than building native apps. Details and the React Native
comparison are in the ADR.
""",
        merged="2025-08-05 14:00",
        labels=["adr", "frontend", "mobile"],
        reviews=[
            approve(
                "sam-whitfield",
                "2025-07-30 11:00",
                "Approve. iOS Safari is the risk (no Background Sync, storage eviction). Let's "
                "make sure we compress photos client-side before they hit IndexedDB.",
            ),
            approve(
                "maya-chen",
                "2025-08-05 13:30",
                "Approved. Two frontend engineers can't maintain a web app plus two native "
                "apps. If offline photo loss keeps happening after this ships, we revisit.",
                key="adr-0008-maya-approve",
            ),
        ],
        comments=[
            c(
                "ines-duarte",
                "2025-07-31 09:20",
                "From the product side: none of the pilot firms asked for an app store app. "
                "They asked for 'works in the basement' and 'doesn't lose my photos'. If the "
                "PWA does that, I'm happy. App store presence can wait.",
                key="adr-0008-ines",
            ),
        ],
    ),
    PR(
        "pr-graphql",
        "ADR proposal: GraphQL for the public API",
        "sam-whitfield",
        "2025-09-03 10:00",
        """
The report editor fetches a lot of nested data (report → sections → items → findings →
photos) and we have N+1 request waterfalls on slow connections. GraphQL would let the
client ask for exactly what it needs in one request, and partners could use the same API.
""",
        closed="2025-09-12 16:00",
        labels=["adr", "api"],
        reviews=[
            comment_review(
                "tomas-rivera",
                "2025-09-04 10:00",
                "I see the waterfall problem, but we can fix that with a `?include=` parameter "
                "on `/v1/reports/{id}`. GraphQL makes HTTP caching and the offline cache much "
                "harder (everything is a POST to one URL), and we just published ADR-0007.",
            ),
        ],
        comments=[
            c(
                "maya-chen",
                "2025-09-12 15:50",
                "Closing this. Decision: no GraphQL for now. We stay with REST + `/v1` "
                "(ADR-0007) and add `include=` for nested resources. Offline caching is the "
                "deciding factor; the service worker can't cache POSTs to `/graphql` "
                "sensibly. We can reopen if partners ask for it.",
                key="graphql-maya-close",
            ),
        ],
    ),
    PR(
        "pr-oncall",
        "Add on-call guide",
        "kofi-mensah",
        "2025-09-15 10:00",
        "First version of the on-call rotation and runbooks.",
        merged="2025-09-16 11:00",
        labels=["docs", "infra"],
        reviews=[approve("tomas-rivera", "2025-09-16 10:30")],
    ),
    PR(
        "pr-adr-0009",
        "ADR-0009: UUIDv7 primary keys for new tables",
        "priya-natarajan",
        "2025-09-26 10:00",
        """
The offline outbox creates findings and photos on the device, so they need ids before the
server sees them. Proposal: UUIDv7 for new tables. Benchmarks on a copy of `photos` (40M
rows): UUIDv4 inserts were 2.3x slower than bigint because of index page splits; UUIDv7 was
within 8% of bigint.
""",
        merged="2025-10-02 12:00",
        labels=["adr", "database"],
        reviews=[
            approve(
                "tomas-rivera",
                "2025-09-29 09:00",
                "Approve. Let's not migrate existing tables, not worth the risk.",
            ),
            approve("maya-chen", "2025-10-02 11:00"),
        ],
    ),
    Issue(
        "issue-adr-approvals",
        "Who needs to approve an ADR?",
        "priya-natarajan",
        "2025-09-24 09:00",
        """
I'm about to open my first ADR (UUIDv7 keys). ADR-0001 says "CTO + an engineer who will
own the change". Does that mean two approvals on the PR? And do vendor ADRs need Inês?
""",
        closed="2025-09-25 12:00",
        state_reason="completed",
        labels=["question", "process"],
        comments=[
            c(
                "maya-chen",
                "2025-09-24 11:00",
                "Yes: two approvals, mine plus an owner. For vendors over $500/month, Inês "
                "should 👍 the PR too. I'll write that into code-review.md.",
                key="adr-approvals-maya",
            ),
            c(
                "ines-duarte",
                "2025-09-24 12:30",
                "Works for me. Ping me on anything with a monthly bill.",
            ),
            c("priya-natarajan", "2025-09-25 11:50", "Thanks, clear now."),
        ],
    ),
    PR(
        "pr-code-review-vendors",
        "Code review: vendor ADRs need CEO sign-off",
        "maya-chen",
        "2025-09-25 10:00",
        "From {#issue-adr-approvals}: vendor decisions over $500/month need a 👍 from Inês.",
        merged="2025-09-25 11:45",
        labels=["docs", "process"],
        reviews=[approve("ines-duarte", "2025-09-25 11:30")],
    ),
    PR(
        "pr-adr-0010",
        "ADR-0010: Move from Heroku to AWS (ECS Fargate + RDS)",
        "kofi-mensah",
        "2025-11-10 10:00",
        """
ADR-0003 said we'd revisit Heroku when the bill passed ~$2k/month or a customer needed
private networking. Both happened:

- October bill: $2,912 (Performance-M dynos for PDF workers after the R14 errors)
- Brightwell Surveys (enterprise pilot) needs VPC peering → Heroku Private Spaces

Estimate on AWS (Fargate + RDS + ElastiCache + ALB): ~$1,700/month. Migration plan is in
{api#issue-aws-plan}. This PR adds ADR-0010 and marks ADR-0003 as superseded.
""",
        merged="2025-11-18 15:00",
        labels=["adr", "infra"],
        reviews=[
            approve(
                "tomas-rivera",
                "2025-11-12 10:00",
                "Approve. I wrote ADR-0003 and its exit criteria are met, so this is the "
                "plan working as intended.",
                key="adr-0010-tomas-approve",
            ),
            approve(
                "maya-chen",
                "2025-11-18 14:30",
                "Approved. Kofi owns the migration; target is Heroku off by early December.",
                key="adr-0010-maya-approve",
            ),
        ],
        comments=[
            c(
                "ines-duarte",
                "2025-11-11 09:00",
                "👍 from me on cost. Saving ~$1.2k/month and unblocking Brightwell is an easy yes.",
                key="adr-0010-ines",
            ),
        ],
    ),
    PR(
        "pr-tech-radar",
        "Add tech radar",
        "maya-chen",
        "2026-01-08 10:00",
        "A one-page view of what we use, what we're trying and what we've retired.",
        merged="2026-01-09 10:00",
        labels=["docs"],
        reviews=[approve("kofi-mensah", "2026-01-09 09:00")],
    ),
    PR(
        "pr-adr-0011",
        "ADR-0011: TanStack Query for server state (supersedes ADR-0006)",
        "lena-park",
        "2026-02-03 10:00",
        """
Follow-up to {web#issue-redux-boilerplate}. Most of our Redux store is cached server data
and we hand-roll loading flags, invalidation and optimistic updates in every slice.

Proposal: TanStack Query for server state (persisted to IndexedDB for offline), Zustand
for the bits of real client state, remove Redux screen by screen. Marks ADR-0006 as
superseded.
""",
        merged="2026-02-10 11:00",
        labels=["adr", "frontend"],
        reviews=[
            approve(
                "sam-whitfield",
                "2026-02-09 16:00",
                "Approve. I wrote ADR-0006 and it was the right call for a 2-person team with "
                "a complex editor, but the editor state is now a small part of the store. The "
                "reports list migration in the spike removed 600 lines.",
                key="adr-0011-sam-approve",
            ),
        ],
        comments=[
            c(
                "maya-chen",
                "2026-02-05 10:00",
                "No objection. Sam and Lena own the frontend, so I'll leave the approval to "
                "Sam. Please keep the offline outbox out of TanStack's cache, it needs to be "
                "durable on its own.",
                key="adr-0011-maya",
            ),
        ],
    ),
    PR(
        "pr-adr-0012",
        "ADR-0012: Replace Elasticsearch with Postgres full-text search",
        "priya-natarajan",
        "2026-03-09 10:00",
        """
Proposal to supersede ADR-0005.

Background: the 2025-12-04 reindex incident (postmortem in `larkspur/api`), the
evaluation in {api#issue-eval-pg-fts} and the benchmark spike in {api#pr-fts-spike}.

TL;DR: 2.1M reports (not 10M), p95 38 ms on Postgres FTS vs 22 ms on ES, budget 150 ms.
ES costs $1,380/month and we maintain permission filtering twice. Plan: tsvector + GIN,
shadow-read for two weeks, cut over behind a flag, delete the cluster.
""",
        merged="2026-03-17 16:00",
        labels=["adr", "search"],
        reviews=[
            comment_review(
                "kofi-mensah",
                "2026-03-10 09:30",
                "",
                comments=[
                    rc(
                        "docs/adr/0012-postgres-full-text-search.md",
                        19,
                        "Confirmed the Elastic Cloud number: $1,380/month after we scaled to "
                        "3 nodes for the reindex in December. Deleting it is the biggest single "
                        "saving on our infra bill this year.",
                        key="adr-0012-kofi-cost",
                    )
                ],
            ),
            approve(
                "tomas-rivera",
                "2026-03-11 10:00",
                "Approve. For the record I argued for Postgres FTS in the ADR-0005 review in "
                "April 2025 :) The numbers back it up now; at 2.1M reports a second datastore "
                "isn't worth it.",
                key="adr-0012-tomas-approve",
            ),
            approve(
                "maya-chen",
                "2026-03-17 15:30",
                "Approved. I made the ES call in 2025 on a 10M-report projection that didn't "
                "happen, and the sync incidents are real ops cost. Postgres FTS it is. Keep "
                "the shadow-read period, and add pg_trgm only if inspectors miss fuzzy address "
                "matching.",
                key="adr-0012-maya-approve",
            ),
        ],
        comments=[
            c(
                "ines-duarte",
                "2026-03-12 09:00",
                "👍 on cost. Please make sure the Brightwell admins hear about any search "
                "changes before cutover, search is the feature they use most.",
            ),
        ],
    ),
    PR(
        "pr-oncall-search",
        "On-call: replace Elasticsearch runbook with Postgres FTS",
        "kofi-mensah",
        "2026-04-15 10:00",
        "The ES cluster is gone ({api#pr-remove-es}). New search runbook points at the GIN index.",
        merged="2026-04-15 15:00",
        labels=["docs", "infra"],
        reviews=[approve("priya-natarajan", "2026-04-15 14:00")],
    ),
    PR(
        "pr-adr-0013",
        "ADR-0013: WorkOS for auth, SSO and SCIM (supersedes ADR-0004)",
        "ravi-kapoor",
        "2026-05-12 10:00",
        """
Enterprise deals are blocked on SAML SSO and SCIM ({api#issue-saml}). Auth0's Enterprise
quote plus SCIM add-on doesn't fit our pricing; WorkOS charges per SSO connection, which we
can pass through in the enterprise plan.

Migration: import Auth0 password hashes into WorkOS, so users keep their passwords. Marks
ADR-0004 as superseded.
""",
        merged="2026-05-20 12:00",
        labels=["adr", "security"],
        reviews=[
            approve(
                "maya-chen",
                "2026-05-19 17:00",
                "Approved. Jonah's ADR-0004 called out the SAML limitation back in 2025; we've "
                "now hit it. WorkOS is the cheaper path to SSO + SCIM.",
                key="adr-0013-maya-approve",
            ),
        ],
        comments=[
            c(
                "ines-duarte",
                "2026-05-13 09:30",
                "👍. Per-connection pricing works: we charge enterprise plans per firm anyway, "
                "so each customer's SSO connection is covered by their plan.",
                key="adr-0013-ines",
            ),
            c(
                "tomas-rivera",
                "2026-05-14 11:00",
                "Make sure we keep the Auth0 tenant read-only for 30 days after cutover in case "
                "we need to roll back.",
            ),
        ],
    ),
    PR(
        "pr-radar-update",
        "Tech radar: retire Elasticsearch and Auth0",
        "maya-chen",
        "2026-06-02 10:00",
        "ES cluster deleted in April, Auth0 removal in progress. Moved both to Retired.",
        merged="2026-06-02 12:00",
        labels=["docs"],
        reviews=[approve("ravi-kapoor", "2026-06-02 11:30")],
    ),
    PR(
        "pr-adr-0014",
        "ADR-0014: Retention of inspection photos and reports",
        "ravi-kapoor",
        "2026-08-04 10:00",
        """
We have no retention policy, and two enterprise security questionnaires asked for one.
Proposal: keep reports and photos 7 years, move photo originals to Glacier Instant
Retrieval after 90 days.
""",
        merged="2026-08-11 10:00",
        labels=["adr", "security", "compliance"],
        reviews=[
            approve(
                "ines-duarte",
                "2026-08-05 09:00",
                "Approve. 7 years matches what our customers' insurers ask for. The 30-day "
                "notice before deletion is important.",
            ),
            approve("maya-chen", "2026-08-10 16:00"),
        ],
    ),
    PR(
        "pr-adr-0015",
        "ADR-0015: Fuzzy address matching with pg_trgm",
        "priya-natarajan",
        "2026-09-01 10:00",
        """
Since the move to Postgres FTS, support tickets about misspelled addresses not matching
({api#issue-fuzzy-address}). Proposal: pg_trgm trigram index on the normalized address,
combined with FTS rank. Not bringing ES back.
""",
        merged="2026-09-08 11:00",
        labels=["adr", "search"],
        reviews=[
            approve(
                "tomas-rivera",
                "2026-09-07 10:00",
                "Approve. This was the follow-up ADR-0012 anticipated.",
            ),
        ],
    ),
    PR(
        "pr-adr-audit-draft",
        "Draft: ADR-0016 Append-only audit log",
        "ravi-kapoor",
        "2026-09-21 10:00",
        """
Draft, not ready for review. Enterprise customers want an audit log of who viewed and
changed reports. Exploring an append-only table vs. event sourcing.
""",
        draft=True,
        labels=["adr", "security"],
        comments=[
            c(
                "tomas-rivera",
                "2026-09-22 09:00",
                "Please not event sourcing for the whole domain :) An append-only "
                "`audit_event` table would cover the questionnaire.",
            ),
        ],
    ),
    Issue(
        "issue-onboarding-typo",
        "Broken link in README",
        "lena-park",
        "2025-06-30 10:00",
        "The link to the code review doc 404s.",
        closed="2025-06-30 12:00",
        state_reason="completed",
        labels=["docs"],
    ),
    Issue(
        "issue-radar-outdated",
        "Tech radar still lists Elasticsearch as Adopt",
        "ravi-kapoor",
        "2026-05-28 10:00",
        "Noticed during onboarding docs review: the radar says Adopt for Elasticsearch, but the "
        "cluster was deleted in April. Also Auth0.",
        closed="2026-06-02 12:00",
        state_reason="completed",
        labels=["docs"],
        comments=[
            c("maya-chen", "2026-05-28 12:00", "Good catch, will update this week."),
            c("ravi-kapoor", "2026-06-02 12:05", "Fixed in {#pr-radar-update}."),
        ],
    ),
    Issue(
        "issue-feature-flags",
        "Do we need an ADR for feature flags?",
        "lena-park",
        "2026-04-07 10:00",
        """
We've used a home-grown `feature_flags` table for the search cutover and the WorkOS
rollout. Should we write an ADR, or look at a vendor (LaunchDarkly, Unleash)?
""",
        labels=["question"],
        comments=[
            c(
                "kofi-mensah",
                "2026-04-07 14:00",
                "LaunchDarkly quoted us ~$700/month at our seat count. The table works fine. I'd "
                "write a short ADR saying we keep it until it hurts.",
            ),
            c("maya-chen", "2026-04-09 10:00", "Agree, but no rush. Leaving open."),
        ],
    ),
]

REPO = Repo(
    name="handbook",
    private=False,
    description="Larkspur engineering handbook and architecture decision records",
    created="2025-03-09 18:00",
    items=ITEMS,
    docs=DOCS,
)
