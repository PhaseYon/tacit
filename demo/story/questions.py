"""Benchmark questions with gold answers and gold sources ("repo:key" refs into the story).

generate.py resolves `gold` refs to html_urls and derives `access` (public only if every
gold source is in the public handbook repo).

`history=True` marks questions where an older, superseded decision exists in the data, so
a system must either return the *current* decision or explain the change over time.
"""

QUESTIONS: list[dict] = [
    # --- current decision vs. superseded history -------------------------------
    {
        "id": "q01",
        "question": "What do we use for report search today, and why did we move off Elasticsearch?",
        "type": "what-changed",
        "answer": "Postgres full-text search (ADR-0012, March 2026, plus pg_trgm from ADR-0015). "
        "ES was dropped because there were 2.1M reports not the projected 10M, keeping it in sync "
        "caused incidents (2025-12-04 outage), permission filtering was duplicated, and it cost "
        "$1,380/month; Postgres FTS had p95 38 ms, within the 150 ms budget.",
        "gold": ["handbook:adr-0012", "api:pr-fts-spike"],
        "history": True,
    },
    {
        "id": "q02",
        "question": "Where is Larkspur hosted?",
        "type": "what-changed",
        "answer": "AWS: ECS Fargate with RDS Postgres, ElastiCache and S3 (ADR-0010, Nov 2025). "
        "Heroku (ADR-0003) was superseded and decommissioned on 2025-12-03.",
        "gold": ["handbook:adr-0010"],
        "history": True,
    },
    {
        "id": "q03",
        "question": "Which authentication provider do we use?",
        "type": "what-changed",
        "answer": "WorkOS (AuthKit, SSO, Directory Sync/SCIM) per ADR-0013 (May 2026). Auth0 "
        "(ADR-0004) was superseded and its SDK removed in June 2026.",
        "gold": ["handbook:adr-0013", "api:pr-remove-auth0"],
        "history": True,
    },
    {
        "id": "q04",
        "question": "How does the web app manage server state?",
        "type": "what-changed",
        "answer": "TanStack Query (persisted to IndexedDB) for server state and Zustand for client "
        "state (ADR-0011, Feb 2026). Redux Toolkit (ADR-0006) was superseded and removed in March 2026.",
        "gold": ["handbook:adr-0011", "web:pr-remove-redux"],
        "history": True,
    },
    {
        "id": "q05",
        "question": "What do we use to render inspection reports to PDF?",
        "type": "what-changed",
        "answer": "Playwright with headless Chromium in the worker-pdf queue, since January 2026. "
        "It replaced wkhtmltopdf.",
        "gold": ["api:pr-playwright", "api:pdf-runbook"],
        "history": True,
    },
    {
        "id": "q06",
        "question": "Is Elasticsearch still used anywhere?",
        "type": "what-changed",
        "answer": "No. The ES client and indexer were removed on 2026-04-14 and the Elastic Cloud "
        "deployment was deleted; the tech radar lists it as Retired.",
        "gold": ["api:pr-remove-es", "api:remove-es-kofi", "handbook:tech-radar"],
        "history": True,
    },
    {
        "id": "q07",
        "question": "How do we handle fuzzy or misspelled address search?",
        "type": "what-changed",
        "answer": "With pg_trgm trigram similarity on a normalized address combined with FTS rank "
        "(ADR-0015, Sept 2026). Not Elasticsearch fuzziness any more.",
        "gold": ["handbook:adr-0015", "api:pr-pg-trgm"],
        "history": True,
    },
    {
        "id": "q08",
        "question": "Are we building a native iOS or Android app?",
        "type": "what-changed",
        "answer": "No. We stay with the offline-first PWA (ADR-0008); re-confirmed on 2026-04-28 by "
        "Inês after a customer request. Revisit at 50 enterprise seats on one customer or an MDM "
        "requirement.",
        "gold": ["web:native-ines-decision", "handbook:adr-0008"],
        "history": True,
    },
    {
        "id": "q09",
        "question": "Which ADR is the current decision for report search: ADR-0005 or ADR-0012?",
        "type": "what-changed",
        "answer": "ADR-0012 (Postgres FTS), amended by ADR-0015. ADR-0005 (Elasticsearch) is "
        "superseded.",
        "gold": ["handbook:adr-0005", "handbook:adr-0012"],
        "history": True,
    },
    {
        "id": "q10",
        "question": "Do we use GraphQL for the public API?",
        "type": "why",
        "answer": "No. REST with /v1 path versioning (ADR-0007). A GraphQL proposal was rejected in "
        "September 2025 because the offline service worker cache can't sensibly cache POSTs to "
        "/graphql; nested data uses ?include= instead.",
        "gold": ["handbook:adr-0007", "handbook:graphql-maya-close"],
        "history": True,
    },
    {
        "id": "q11",
        "question": "How does the PDF worker avoid Chromium cold starts?",
        "type": "what-changed",
        "answer": "It keeps a warm pool of 3 Chromium browsers per task, recycled every 200 renders "
        "(added Feb 2026 after slow-PDF tickets).",
        "gold": ["api:pr-browser-pool", "api:pdf-runbook"],
    },
    # --- who decided ------------------------------------------------------------
    {
        "id": "q12",
        "question": "Who decided to use Elasticsearch for search originally?",
        "type": "who",
        "answer": "Jonah Blake proposed it (ADR-0005) and Maya Chen approved it on 2025-04-22; "
        "Tomás Rivera had raised concerns.",
        "gold": ["handbook:pr-adr-0005", "handbook:adr-0005-maya-approve"],
        "history": True,
    },
    {
        "id": "q13",
        "question": "Who approved replacing Elasticsearch with Postgres full-text search?",
        "type": "who",
        "answer": "Priya Natarajan proposed ADR-0012; Maya Chen and Tomás Rivera approved it "
        "(merged 2026-03-17).",
        "gold": [
            "handbook:pr-adr-0012",
            "handbook:adr-0012-maya-approve",
            "handbook:adr-0012-tomas-approve",
        ],
        "history": True,
    },
    {
        "id": "q14",
        "question": "Who decided our pricing model, and what is it?",
        "type": "who",
        "answer": "Inês Duarte, on 2025-07-08: per seat with 40 reports/month included per seat "
        "and per-report overage; annual plans get two months free.",
        "gold": ["api:pricing-ines-decision"],
    },
    {
        "id": "q15",
        "question": "Who made the call not to build a native iOS app for Harbor & Pine Inspections?",
        "type": "who",
        "answer": "Inês Duarte closed it as not planned on 2026-04-28, with input from Lena, Sam "
        "and Maya.",
        "gold": ["web:native-ines-decision"],
    },
    {
        "id": "q16",
        "question": "Who decided to move off Heroku, and who led the migration?",
        "type": "who",
        "answer": "Kofi Mensah proposed ADR-0010 and led the migration; Maya Chen and Tomás Rivera "
        "approved; Inês Duarte signed off on cost.",
        "gold": ["handbook:pr-adr-0010", "handbook:adr-0010-maya-approve", "api:issue-aws-plan"],
        "history": True,
    },
    {
        "id": "q17",
        "question": "Who decided against GraphQL?",
        "type": "who",
        "answer": "Maya Chen closed Sam Whitfield's GraphQL proposal on 2025-09-12, after Tomás "
        "Rivera argued for REST with ?include=.",
        "gold": ["handbook:graphql-maya-close", "handbook:pr-graphql"],
    },
    {
        "id": "q18",
        "question": "Who approved switching to WorkOS, and who signed off on the cost?",
        "type": "who",
        "answer": "Ravi Kapoor proposed ADR-0013, Maya Chen approved it, and Inês Duarte signed off "
        "on the per-connection pricing.",
        "gold": [
            "handbook:adr-0013-maya-approve",
            "handbook:adr-0013-ines",
            "api:saml-ines-cost",
        ],
        "history": True,
    },
    {
        "id": "q19",
        "question": "Who decided how conflicting offline edits are resolved, and how?",
        "type": "who",
        "answer": "Maya Chen: per-field last-writer-wins plus a conflict banner; CRDTs (Yjs) were "
        "deferred. Lena Park implemented it.",
        "gold": ["web:offline-conflict-maya", "web:pr-field-merge"],
    },
    {
        "id": "q20",
        "question": "Who needs to approve an ADR?",
        "type": "who",
        "answer": "The CTO (Maya Chen) plus an engineer who will own the change; vendor decisions "
        "over $500/month also need a thumbs-up from the CEO (Inês Duarte).",
        "gold": ["handbook:code-review", "handbook:adr-approvals-maya"],
    },
    {
        "id": "q21",
        "question": "Who took over search after Jonah left?",
        "type": "who",
        "answer": "Priya Natarajan (Tomás Rivera took PDFs), per Jonah's handoff notes in Oct 2025.",
        "gold": ["api:pr-jonah-handoff"],
    },
    # --- why ----------------------------------------------------------------------
    {
        "id": "q22",
        "question": "Why did we pick Heroku in the first place?",
        "type": "why",
        "answer": "No platform engineer, git-push deploys and managed Postgres/Redis with backups, to "
        "ship the pilot in April 2025. Exit criteria: bill over ~$2k/month or private networking.",
        "gold": ["handbook:adr-0003", "handbook:pr-adr-0003"],
        "history": True,
    },
    {
        "id": "q23",
        "question": "Why did we move from Heroku to AWS?",
        "type": "why",
        "answer": "The bill hit ~$2,900/month vs ~$1,700 estimated on AWS, PDF workers hit R14/R15 "
        "memory errors on 1 GB dynos, an enterprise customer needed VPC peering (Private Spaces), "
        "and we had a platform engineer.",
        "gold": ["handbook:adr-0010", "handbook:pr-adr-0010"],
        "history": True,
    },
    {
        "id": "q24",
        "question": "Why did we originally choose wkhtmltopdf instead of headless Chrome?",
        "type": "why",
        "answer": "A single static binary that was fast (~1.5 s for 30 pages) and worked on Heroku; "
        "Chromium needed ~400 MB of dependencies and more memory than the 1 GB dynos had.",
        "gold": ["api:pr-wkhtmltopdf", "api:wkhtml-tomas-approve"],
        "history": True,
    },
    {
        "id": "q25",
        "question": "Why did we replace wkhtmltopdf?",
        "type": "why",
        "answer": "It is archived/unmaintained, can't render CSS grid/flexbox (the photo grid broke), "
        "bundles an old Qt WebKit with CVEs, and Fargate's 4 GB PDF worker removed the memory "
        "constraint.",
        "gold": ["api:pr-playwright", "api:issue-wkhtml-broken", "api:wkhtml-ravi-cve"],
        "history": True,
    },
    {
        "id": "q26",
        "question": "Why Celery rather than RQ or Dramatiq for background jobs?",
        "type": "why",
        "answer": "Celery has retries with backoff, beat scheduling and queue routing (PDFs on their "
        "own queue); RQ lacks scheduling and nobody had run Dramatiq in production.",
        "gold": ["api:pr-celery", "api:celery-maya-approve"],
    },
    {
        "id": "q27",
        "question": "Why do new tables use UUIDv7 primary keys?",
        "type": "why",
        "answer": "Offline clients must create ids before the server sees them; sequential ints need "
        "a round trip and leak volume; UUIDv4 fragments B-tree indexes (2.3x slower inserts) while "
        "UUIDv7 is within 8% of bigint.",
        "gold": ["handbook:adr-0009", "handbook:pr-adr-0009"],
    },
    {
        "id": "q28",
        "question": "Why version the API in the URL path instead of a header?",
        "type": "why",
        "answer": "The PWA's service worker cache keys on the URL, so header versions would collide "
        "in the offline cache.",
        "gold": ["handbook:adr-0007", "handbook:adr-0007-sam-cache"],
    },
    {
        "id": "q29",
        "question": "Why did we leave Auth0?",
        "type": "why",
        "answer": "Enterprise customers needed SAML SSO and SCIM; on Auth0 that required the "
        "Enterprise plan (~4x the bill) plus a SCIM add-on. WorkOS's per-connection pricing fits "
        "our per-firm enterprise plans.",
        "gold": ["api:saml-ravi-quote", "handbook:adr-0013"],
        "history": True,
    },
    {
        "id": "q30",
        "question": "Why did we build a PWA instead of native mobile apps?",
        "type": "why",
        "answer": "Inspectors need full offline capture in basements; two frontend engineers can't "
        "maintain web plus two native apps with app store releases; customers asked for offline "
        "reliability, not app store presence.",
        "gold": ["handbook:adr-0008", "handbook:adr-0008-ines"],
    },
    {
        "id": "q31",
        "question": "Why is API rate limiting done in the app instead of AWS WAF?",
        "type": "why",
        "answer": "WAF limits per IP and partners call from shared cloud IPs; we need per-API-key and "
        "per-plan limits only the app knows. WAF stays as a coarse DDoS backstop.",
        "gold": ["api:ratelimit-ravi-reason", "api:pr-rate-limit"],
    },
    {
        "id": "q32",
        "question": "Why did we drop Redux?",
        "type": "why",
        "answer": "About 80% of the store was cached server data with hand-written loading flags, "
        "invalidation and optimistic updates; lots of boilerplate per screen and fragile offline "
        "thunk replay.",
        "gold": ["handbook:adr-0011", "web:issue-redux-boilerplate"],
        "history": True,
    },
    {
        "id": "q33",
        "question": "Why do photo originals move to Glacier after 90 days?",
        "type": "why",
        "answer": "Photos are 85% of storage cost and rarely opened after 90 days; reports must be "
        "kept 7 years as legal records, so originals go to Glacier Instant Retrieval (ADR-0014).",
        "gold": ["handbook:adr-0014", "api:pr-glacier"],
    },
    # --- when ---------------------------------------------------------------------
    {
        "id": "q34",
        "question": "When was Heroku decommissioned?",
        "type": "when",
        "answer": "2025-12-03.",
        "gold": ["api:pr-cutover", "handbook:adr-0010"],
        "history": True,
    },
    {
        "id": "q35",
        "question": "When did production search switch to Postgres, and when was Elasticsearch removed?",
        "type": "when",
        "answer": "Cutover merged 2026-03-24 (search_backend flag defaulting to postgres); ES code "
        "removed and cluster deleted on 2026-04-14.",
        "gold": ["api:pr-cutover-fts", "api:pr-remove-es"],
        "history": True,
    },
    {
        "id": "q36",
        "question": "When was the search reindex outage and how bad was it?",
        "type": "when",
        "answer": "2025-12-04, 02:10 to 11:20 UTC: about 18% of reports (~340k) were missing from "
        "search; no data loss.",
        "gold": ["api:postmortem", "api:issue-search-missing"],
    },
    {
        "id": "q37",
        "question": "When were users migrated from Auth0 to WorkOS?",
        "type": "when",
        "answer": "Tuesday 2026-06-09 at 07:00 UTC, importing password hashes so nobody had to reset.",
        "gold": ["api:pr-auth0-migration"],
    },
    {
        "id": "q38",
        "question": "When did Stripe billing ship, and what pricing does it implement?",
        "type": "when",
        "answer": "Merged 2025-07-15: per-seat subscriptions with 40 reports/month per seat and "
        "metered per-report overage.",
        "gold": ["api:pr-stripe", "api:pricing-ines-decision"],
    },
    # --- evidence -----------------------------------------------------------------
    {
        "id": "q39",
        "question": "What's the evidence that Postgres full-text search is fast enough for us?",
        "type": "evidence",
        "answer": "The Feb 2026 spike on 2.1M reports: p95 38 ms vs 22 ms on ES (budget 150 ms) with "
        "91% top-10 overlap; the shadow-read on 61k real queries: p95 41 ms, 93% overlap.",
        "gold": ["api:pr-fts-spike", "api:shadow-results"],
    },
    {
        "id": "q40",
        "question": "What was the root cause of the December 2025 search outage?",
        "type": "evidence",
        "answer": "The reindex swapped the alias as soon as the bulk load was enqueued, not finished, "
        "and the 2-node Elastic Cloud cluster then hit its disk watermark and stalled.",
        "gold": ["api:postmortem", "api:search-missing-root-cause"],
    },
    {
        "id": "q41",
        "question": "How much money did removing Elasticsearch save?",
        "type": "evidence",
        "answer": "About $1,380 per month (the 3-node Elastic Cloud deployment).",
        "gold": ["api:remove-es-kofi", "handbook:adr-0012-kofi-cost", "api:eval-kofi-cost"],
    },
    {
        "id": "q42",
        "question": "Did anyone object to Elasticsearch when it was chosen?",
        "type": "evidence",
        "answer": "Yes. Tomás Rivera requested changes on ADR-0005: a second datastore to sync, a "
        "reindex procedure and duplicated permission rules; he suggested Postgres FTS.",
        "gold": ["handbook:adr-0005-tomas-concern"],
        "history": True,
    },
    {
        "id": "q43",
        "question": "What evidence is there that permission filtering in Elasticsearch drifted from Postgres?",
        "type": "evidence",
        "answer": "Sept 2025: inspectors who changed teams still saw the old team's reports in search; "
        "the March 2026 shadow-read found 14 ES hits that Postgres correctly hid.",
        "gold": ["api:issue-es-permissions", "api:shadow-results"],
    },
    # --- multi-hop ----------------------------------------------------------------
    {
        "id": "q44",
        "question": "What did the engineer who proposed Elasticsearch say about it when he left, and "
        "what happened to it afterwards?",
        "type": "multi-hop",
        "answer": "Jonah Blake's handoff (Oct 2025) said that at ~1.4M reports Postgres FTS might have "
        "been enough. In March 2026 Priya replaced ES with Postgres FTS (ADR-0012).",
        "gold": ["api:pr-jonah-handoff", "handbook:adr-0012"],
        "history": True,
    },
    {
        "id": "q45",
        "question": "Which constraint led us to wkhtmltopdf, and which infrastructure change removed it?",
        "type": "multi-hop",
        "answer": "Chromium didn't fit in 1 GB Heroku dynos; the move to ECS Fargate (ADR-0010) gave "
        "the PDF worker 4 GB, which made the Playwright switch possible.",
        "gold": ["api:pr-wkhtmltopdf", "api:pr-terraform", "api:pr-playwright"],
        "history": True,
    },
    {
        "id": "q46",
        "question": "Which postmortem action item led to an ADR, and what did that ADR decide?",
        "type": "multi-hop",
        "answer": "Action item 3 of the 2025-12-04 search postmortem ('evaluate whether we need "
        "Elasticsearch') led to the evaluation issue and ADR-0012: replace ES with Postgres FTS.",
        "gold": ["api:postmortem", "api:issue-eval-pg-fts", "handbook:adr-0012"],
        "history": True,
    },
    {
        "id": "q47",
        "question": "ADR-0004 came with a warning about a limitation. Did it come true, and what did we do?",
        "type": "multi-hop",
        "answer": "Yes: SAML was only on Auth0's top plans. In 2026 enterprise deals required SAML "
        "and SCIM, so we moved to WorkOS (ADR-0013).",
        "gold": [
            "handbook:adr-0004-saml-warning",
            "handbook:adr-0004",
            "api:issue-saml",
            "handbook:adr-0013",
        ],
        "history": True,
    },
    {
        "id": "q48",
        "question": "What follow-up did the move to Postgres search require, and who raised it?",
        "type": "multi-hop",
        "answer": "Misspelled addresses stopped matching; Inês raised it from support tickets in July "
        "2026 and Priya added pg_trgm fuzzy matching (ADR-0015).",
        "gold": ["api:issue-fuzzy-address", "handbook:adr-0015"],
        "history": True,
    },
    {
        "id": "q49",
        "question": "How is the UUIDv7 decision connected to the offline photo outbox?",
        "type": "multi-hop",
        "answer": "Outbox entries carry client-generated ids so the API can dedupe retried uploads; "
        "ADR-0009 chose UUIDv7 so clients can create those ids offline without collisions.",
        "gold": ["web:pr-offline-sw", "handbook:adr-0009"],
    },
    {
        "id": "q50",
        "question": "Did Tomás's 2025 concern about Elasticsearch turn out to matter?",
        "type": "multi-hop",
        "answer": "Yes. Sync problems caused three search incidents (mapping limit, team-move "
        "permissions, the Dec 2025 outage) and ES was replaced; Tomás approved ADR-0012 noting he "
        "had argued for Postgres FTS in April 2025.",
        "gold": [
            "handbook:adr-0005-tomas-concern",
            "api:search-missing-tomas",
            "handbook:adr-0012-tomas-approve",
        ],
        "history": True,
    },
    # --- undecided ----------------------------------------------------------------
    {
        "id": "q51",
        "question": "Have we decided to move background jobs off Celery to a Postgres-backed queue?",
        "type": "evidence",
        "answer": "No. It's an open discussion (Sept 2026); Redis stays anyway for rate limiting and "
        "the team prefers to wait for a concrete problem.",
        "gold": ["api:issue-pg-queue"],
    },
    {
        "id": "q52",
        "question": "Do we use a feature flag vendor?",
        "type": "evidence",
        "answer": "No. We use a home-grown feature_flags table; LaunchDarkly was quoted at ~$700/month "
        "and the question is still open.",
        "gold": ["handbook:issue-feature-flags"],
    },
]
