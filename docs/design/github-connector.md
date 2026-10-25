# Design: GitHub connector (build step 2)

Status: draft for review · Owner: Ahmed Fezan · Date: 2026-10-24

This connector syncs PRs, issues, their discussion and design docs from GitHub into
`source_document` rows. Each row has an exact citation URL and a repo-level ACL that
Postgres enforces. Search and chunking (step 3) and Graphiti extraction (step 4) read
these rows. This step makes **no LLM calls**.

Non-goals: code search, diffs, CI data, GitHub Discussions (needs GraphQL, later), and
live MCP lookups into GitHub (that's the "MCP in" side and has nothing to do with bulk sync).

---

## 1. Auth model: GitHub App

**Decision: a GitHub App**, installed per GitHub account or org and linked to one Tacit workspace.

| Option | Verdict |
|---|---|
| **GitHub App** | ✅ Fine-grained read-only permissions; the customer picks which repos; short-lived installation tokens; one app-level webhook for all installs; rate limit scales with the org (5k/h minimum, up to 12.5k/h). |
| OAuth App | ❌ Acts as a user, with broad `repo` scope covering everything that user can see. The webhook has to be created per repo or org. If the user leaves, the token dies. |
| PAT | ❌ Ties sync to one person, has no install flow, is manual to rotate, and can't scale to multiple tenants. Only for one-off scripts. |

The same App also handles **user identity linking**. GitHub Apps support the OAuth web flow, so
the App's client ID and secret go into Better Auth's GitHub social provider. A user
"connects GitHub" once, and we get a *verified* GitHub user ID (see §6).

**Repository permissions (all read-only):**

| Permission | Why |
|---|---|
| Metadata | Mandatory. Repo list, collaborators (ACL sync). |
| Issues | Issues and issue comments (including PR conversation comments). |
| Pull requests | PRs, reviews, review comments. |
| Contents | Markdown docs via the git tree and blob APIs, and the `push` event. |

**Organization permissions:** Members (read), to receive `membership`, `team` and
`organization` events so ACLs refresh when team membership changes.
**Account permissions:** Email addresses (read), which Better Auth needs to read the
user's email during linking.

**Webhook events:** `installation`, `installation_repositories`, `repository`,
`issues`, `issue_comment`, `pull_request`, `pull_request_review`,
`pull_request_review_comment`, `push`, `member`, `membership`, `team`, `organization`,
`github_app_authorization` (a user revoked the App, so we drop their link).

App settings: "Request user authorization (OAuth) during installation" **off**. When
it's on, GitHub sends the installer to the OAuth callback instead of the Setup URL
(see "Install flow" for how we verify the installer instead). Setup URL is
`{API}/github/setup`. Webhook URL is `{API}/webhooks/github`. Store the App config as a
**manifest** (`infra/github/app-manifest.json`) so dev and prod Apps can be recreated
with the manifest flow instead of clicking through settings.

## 2. Libraries

**Decision: [githubkit](https://github.com/yanyongyu/githubkit) `~=0.16`** (0.16.1, released
2026-08-14) for REST, GitHub App auth and webhook verification and parsing.

It does what we'd otherwise have to write by hand:
- `AppAuthStrategy` → `.with_auth(app.auth.as_installation(id))` mints installation tokens
  and caches them until they expire.
- `githubkit.webhooks.verify(secret, body, sig256)` (constant-time HMAC check) and
  `parse(event_name, body)` return typed Pydantic models for every event.
- Both sync and async calls, fully typed, generated from GitHub's OpenAPI schema.
- `auto_retry` retries rate-limit errors (403/429, waiting for GitHub's suggested time)
  and 5xx errors. It's customizable with `RetryChainDecision` / custom `RetryOption`.
- `http_cache=True` (Hishel) sends ETag / `If-None-Match` automatically.
  `AsyncRedisCacheStrategy` shares the HTTP cache *and* installation tokens across
  processes in Redis. `throttler` caps how many requests one process runs at once.
- Built-in pagination, plus GraphQL if we need it later.

| Rejected | Why |
|---|---|
| PyGithub 2.10 (2026-08) | Sync only, lazy objects hide N+1 calls, no Redis-shared cache. Well maintained, but the wrong shape for async workers. |
| gidgethub 6.0 (2026-09) | Async and maintained, but untyped dicts, no retry or cache. We'd rebuild half of githubkit. |
| Raw httpx | Everything above becomes custom code. |

Risk: githubkit is pre-1.0 with one main maintainer. Mitigation: pin the minor version,
and keep it behind `tacit_api/connectors/github/client.py` so swapping it touches one module.

Other libraries: `celery[redis]` for workers and the beat schedule, `respx` to mock httpx
in tests, and the `@octokit/webhooks-examples` payloads as test fixtures.

## 3. What we ingest (and what we skip)

Rule: ingest text where people explain **why**. Skip machine output and code.

| Ingest (v1) | Kind | Notes |
|---|---|---|
| Pull requests | `pull_request` | Title, body, state, `merged_at`, labels, author. Store review approvals (who approved) in PR `metadata`: a "who decided" signal at no extra cost. |
| PR reviews with a body | `pr_review` | Reviews with no text (bare approvals) only update PR metadata. |
| PR review comments | `pr_review_comment` | Body, `path`, `line`, diff hunk cut to about 20 lines for context. |
| Issues | `issue` | Title, body, state, `state_reason`, labels. |
| Issue and PR conversation comments | `issue_comment` | One row per comment, so citations point at the exact comment. |
| Markdown docs on the default branch | `markdown_file` | Globs (configurable per repo): `README.md`, `docs/**/*.md`, `**/adr/**/*.md`, `**/ADR-*.md`, `**/rfcs/**/*.md`. ADRs are the best "why" source there is. |

**Skip (v1):** code files and diffs (expensive and say little about *why*), commit
messages (squash merges repeat the PR title, and direct pushes are rare; revisit after
the benchmark), CI and check runs, releases, reactions, wiki (no API), Discussions
(GraphQL, later), and **bot authors** (`user.type == "Bot"`: dependabot, renovate,
codecov). Bodies bigger than 64 KB are cut off and flagged in metadata.

**Cost reasoning:** this step costs only GitHub API calls. We store a `token_count`
for every document from day one (computed with the embedding model's tokenizer), so
step 3/4 cost is predictable: embedding everything is cheap, but Graphiti extraction
(several LLM calls per episode) will be **gated**. Only merged PRs together with their
review thread, closed issues with ≥2 human comments, and markdown docs become
episodes. Edits cause re-extraction only when `content_hash` changes. We'll report
per-repo totals (docs, tokens, estimated extraction cost) from a SQL view before
turning extraction on.

## 4. Data model

Alembic migration `0002_github_connector`. Ids are `uuid` (`gen_random_uuid()`), GitHub
ids are `bigint`, and `workspace_id text references organization(id) on delete cascade`.
We keep two generic tables (`source_container`, `container_access`) so Slack channels
and Notion teamspaces fit in step 6 without changing the retrieval filter.

```text
github_installation                                  RLS: workspace
  id uuid pk, workspace_id text,
  installation_id bigint UNIQUE          -- one install → exactly one workspace
  account_id bigint, account_login text, account_type text  -- User | Organization
  repository_selection text              -- all | selected
  permissions jsonb, events jsonb        -- as granted; checked against what we need
  installed_by_user_id text → user(id)
  suspended_at, deleted_at, created_at, updated_at timestamptz

source_container                                     RLS: workspace
  id uuid pk, workspace_id text,
  source text                            -- github | slack | notion
  external_id text                       -- github: repo id
  name text, url text                    -- "owner/repo", html_url
  visibility text                        -- public | private | internal
  is_active bool                         -- false once removed from install / archived+opted out
  acl_synced_at timestamptz              -- NULL ⇒ nobody but public rules can read
  UNIQUE (workspace_id, source, external_id)

github_repository                                    RLS: workspace
  container_id uuid pk → source_container(id) on delete cascade
  workspace_id text, installation_id bigint → github_installation(installation_id)
  repo_id bigint, node_id text, full_name text, default_branch text, archived bool
  sync_enabled bool default true, doc_globs text[] null   -- null ⇒ defaults
  backfill_status text                   -- pending | running | done | failed
  last_reconciled_at timestamptz
  UNIQUE (workspace_id, repo_id)

github_sync_cursor                                   RLS: workspace
  container_id uuid, resource text       -- issues | issue_comments | review_comments | tree
  since timestamptz, next_url text null  -- next_url set while a backfill is mid-pagination
  etag text null, updated_at timestamptz
  PK (container_id, resource)

source_document                                      RLS: workspace + ACL (restrictive)
  id uuid pk, workspace_id text, container_id uuid → source_container
  source text, kind text                 -- see §3
  external_id text                       -- GitHub node_id; files: "{repo_id}:{path}"
  parent_id uuid null → source_document  -- comment/review → its PR or issue
  url text NOT NULL                      -- exact citation: comment permalink, blob URL at commit sha
  title text null, body text             -- normalized markdown
  author_external_id text, author_login text
  source_created_at, source_updated_at timestamptz
  metadata jsonb                         -- state, labels, merged_at, approvals, path, line, sha …
  content_hash bytea                     -- sha256 of canonical content (see §5)
  processed_hash bytea null              -- hash last chunked/embedded/extracted (steps 3–4)
  token_count int
  deleted_at timestamptz null            -- tombstone; hidden from search, purged later
  ingested_at, updated_at timestamptz
  UNIQUE (workspace_id, source, external_id)
  INDEX (workspace_id, container_id), INDEX (parent_id), partial INDEX WHERE processed_hash IS DISTINCT FROM content_hash

external_identity                                    RLS: workspace
  id uuid pk, workspace_id text, source text, external_id text  -- GitHub user id
  login text, display_name text, avatar_url text, is_bot bool
  person_id uuid null                    -- cross-tool person entity, added in step 4/6
  UNIQUE (workspace_id, source, external_id)

container_access                                     RLS: workspace
  container_id uuid → source_container on delete cascade, user_id text → user(id) on delete cascade
  workspace_id text, permission text     -- GitHub role_name (read|triage|write|maintain|admin)
  via_external_id text, synced_at timestamptz
  PK (container_id, user_id)

webhook_delivery                                     NO RLS; worker-only, no tacit_api grant
  delivery_id uuid pk                    -- X-GitHub-Delivery
  event text, action text, installation_id bigint
  status text                            -- queued | done | ignored | failed
  attempts int, error text, payload jsonb, received_at, processed_at timestamptz
  -- payloads purged after 14 days by a beat task
```

**Roles and RLS** (following the conventions in `migrations/rls.py`):
- Every table above except `webhook_delivery` gets `enable_workspace_rls()`.
- **New role `tacit_worker`** for Celery and the webhook receiver. Its policy is
  `workspace_id = app.current_workspace_id()` without `is_member()`, because jobs have
  no user. Each job calls `set_config('app.workspace_id', …, true)` per transaction.
  Installation → workspace lookup goes through a `SECURITY DEFINER` function
  `app.workspace_for_installation(bigint)`, so the worker never needs cross-workspace reads.
  (This needs a line in `infra/postgres/init/01-roles.sh`. Coordinate with step 1.)
- `tacit_api` gets **SELECT only** on connector tables. All writes go through `tacit_worker`.
- **ACL in the database:** on `source_document` (and the chunk tables later), add a
  **`AS RESTRICTIVE`** policy for `tacit_api`: `USING (app.can_read_container(container_id))`.
  It must be restrictive: permissive policies are OR'ed together, so a permissive one
  would *widen* access. `app.can_read_container` (SECURITY DEFINER) returns true if the
  container is active and either public, or has a `container_access` row for
  `app.current_user_id()` and its `acl_synced_at` is under 6 hours old. This way the
  web app, the agent and MCP all get the same filter, and a bug in app code can't leak rows.
- **Linked identities:** add a column-level grant `GRANT SELECT ("userId","providerId","accountId") ON account TO tacit_worker`.
  This follows the same pattern as the `user` grant in 0001, and never exposes token columns.

## 5. Sync design

```text
GitHub ──webhook──▶ FastAPI /webhooks/github ──▶ webhook_delivery (dedupe) ──▶ Celery queue
                    verify HMAC, insert, enqueue, 202 in <100 ms          │
Celery beat ──▶ reconcile / ACL refresh / redeliver failed ───────────────┤
                                                                           ▼
                        workers: githubkit (installation token, Redis cache) → normalize → upsert
```

**Install flow**
1. In a workspace, an admin clicks "Connect GitHub". The API issues a signed `state` (JWT:
   workspace_id, user_id, nonce, 10-minute expiry) and redirects to
   `github.com/apps/<slug>/installations/new?state=…`.
2. GitHub redirects to `/github/setup?installation_id&setup_action&state`. **The
   `installation_id` in that URL is unauthenticated.** We verify `state`, then check
   the installation against the user's *linked, OAuth-verified* GitHub id (connecting
   requires linking GitHub first). A personal install's account must be that user; for an
   org install, `GET /orgs/{org}/memberships/{login}` (installation token) must show an
   active admin. *(Implementation note, 2026-10-25: this replaces the original
   code-exchange plan, which doesn't work because GitHub skips the Setup URL when
   OAuth-on-install is enabled.)*
3. Upsert `github_installation` (insert fails if the install already belongs to
   another workspace). The `installation.created` webhook may arrive before or after
   this; whichever comes second finds the row and moves on. Both paths enqueue
   `sync_installation_repos`.
4. `sync_installation_repos` lists `GET /installation/repositories` and upserts
   `source_container` and `github_repository`. For each repo: `sync_repo_acl`, then `backfill_repo`.

**Webhook receiver** (`POST /webhooks/github`): read the raw body, `verify()` it (401 on
failure), then `INSERT … ON CONFLICT (delivery_id) DO NOTHING`. Duplicates get 200 and
are dropped. Then enqueue `process_delivery(delivery_id)` and return 202. GitHub times out
after 10 s and **does not redeliver failed deliveries**, so the receiver does no GitHub
calls and no heavy DB work.

**Backfill** (one Celery task per page, resumable): the work is split into small
idempotent tasks. Each fetches one page, upserts it, saves `next_url` in
`github_sync_cursor` and enqueues the next page, so a crash resumes where it stopped.
We use repo-wide list endpoints to avoid N+1 calls:
- `GET /repos/{o}/{r}/issues?state=all&sort=updated&direction=asc&since=` returns issues
  **and** PRs (the `pull_request.merged_at` field is included).
- `GET /repos/{o}/{r}/issues/comments?sort=updated&since=` covers issue and PR
  conversation comments.
- `GET /repos/{o}/{r}/pulls/comments?sort=updated&since=` covers review comments.
- `GET /repos/{o}/{r}/pulls/{n}/reviews` runs only for PRs whose `updated_at` moved
  (there's no repo-wide reviews endpoint).
- `GET /repos/{o}/{r}/git/trees/{default_branch}?recursive=1` → filter by glob → fetch
  only blobs whose **blob SHA** changed. Git already content-addresses files, so
  unchanged docs cost nothing.

After the last page, `since` = max `updated_at` seen and `backfill_status = done`.
Order: issues/PRs before comments, so `parent_id` resolves. Comments whose parent is
still missing get re-queued once, and are otherwise linked by the reconcile pass.

**Incremental (webhooks):** issue, comment, PR and review payloads already contain
the full object, so we normalize and upsert straight from the payload with **no API call**.
`push` to the default branch with changed `.md` paths matching the globs fetches only
those blobs. `deleted` actions and `issues.transferred` set `deleted_at`.
`installation_repositories.removed`, `repository.deleted`/`archived` (unless opted in)
and `installation.deleted`/`suspend` set `is_active = false` and delete
`container_access` **first** (so access is gone immediately), then soft-delete docs.
Purge after 30 days, or right away on uninstall (open question).

**Reconcile (beat):**
- Every 6 h per repo: rerun the `since` list calls from `cursor.since − 10 min`
  (overlap absorbs clock skew). This catches anything webhooks missed.
- Hourly: `GET /app/hook/deliveries`, then `POST …/attempts` to redeliver failed ones
  (GitHub keeps them for about 3 days).
- Weekly: tombstone sweep. Re-list IDs per resource and mark rows missing upstream
  (`since` listings never show deletes).

**Idempotency and ordering**
- Webhooks are deduplicated by `delivery_id`. Every write is an upsert on
  `(workspace_id, source, external_id)`.
- The upsert's `WHERE source_document.source_updated_at <= excluded.source_updated_at`
  stops an older out-of-order event from overwriting a newer one.
- Celery `acks_late=True` and `task_reject_on_worker_lost=True`. Tasks are safe to
  re-run by construction.

**Content hashing:** `content_hash = sha256(canonical_json({kind, title, body_normalized,
state, labels_sorted, merged, path}))`. Body normalization: `\r\n→\n`, strip trailing
whitespace. Volatile fields (reaction and comment counts, `updated_at`) are **excluded**,
so emoji or label noise doesn't trigger reprocessing. Steps 3 and 4 process only rows
`WHERE processed_hash IS DISTINCT FROM content_hash`, which is how we "never reprocess
unchanged content".

**Rate limits and ETags**
- One `GitHub` client per installation, with `http_cache=True` and
  `AsyncRedisCacheStrategy`. Tokens and ETag cache entries are shared by all workers.
  Conditional 304s don't count against the rate limit, which makes hourly ACL refresh
  and tree polls nearly free. (`since` listings change their URL each time, so they
  rarely hit the cache. That's expected.)
- Retry policy: `RetryChainDecision(RetryRateLimit(max_retry=1), RetryServerError(max_retry=3))`,
  wrapped so in-process waits are **capped at 60 s**. Longer waits raise, and the
  Celery task does `self.retry(countdown=reset_at - now + jitter)` instead of tying up
  a worker.
- Budget split: backfill tasks check `x-ratelimit-remaining`, and below 20 % they
  reschedule until `x-ratelimit-reset`. That leaves headroom for webhook-driven and ACL
  work. Queues: `github_realtime` (webhooks, ACL) and `github_backfill` (concurrency 2).
- Celery is sync. Tasks are thin wrappers that call `asyncio.run(handler(...))`. Workers
  use SQLAlchemy `NullPool`, because async connections can't be reused across event loops.

## 6. Permission sync

**Rule: a Tacit user can read a private repo's documents only if their *verified*
GitHub account currently has access to that repo on GitHub, they're a member of the
workspace, and the ACL is fresh.** If any of these is missing, access is denied.

**Identity:** a user links GitHub through Better Auth's GitHub provider, which writes
`account(providerId='github', accountId=<GitHub user id>)`. Only this OAuth-proven
link grants access. A matching email or login is never enough. Unverified matches may
fill `external_identity.person_id` for the graph and display, never for ACLs. This is
the GitHub half of "one identity per person". Step 6 adds Slack and Notion links to the same `person`.

**Computing access (`sync_repo_acl(container_id)`):**
1. With the installation token, `GET /repos/{o}/{r}/collaborators?affiliation=all`
   (paginated, ETag-cached). For org repos this includes outside collaborators, direct
   members, members with access through teams (including child teams) or base
   permissions, and owners. Each entry has `role_name`.
2. Join on `account.accountId` (provider github) ∩ `member` of this workspace → desired set.
3. In one transaction, diff against `container_access`, insert or delete rows, and set
   `acl_synced_at = now()`.
4. Public repos: readable by every workspace member (the content is public anyway).
   Visibility comes from `repository` events and the repo listing.

**Rejected:** (a) installation-level access ("anyone in the workspace sees every
installed repo"), because it leaks private repos to teammates who lack GitHub access.
(b) Using per-user tokens with `GET /user/installations/{id}/repositories` as the
source of truth. That's accurate, but we'd have to store and refresh each user's
8-hour token, and we can't refresh access for users who are offline. It may come back
later as a login-time cross-check.

**Refreshing stale permissions:**
- Events → enqueue ACL resync for affected repos: `member` (collaborator
  added/removed/edited), `membership` (team members), `team` (added_to/removed_from
  repository, edited permission), `organization.member_added/removed`,
  `repository.privatized/publicized/transferred`, `installation_repositories`,
  `github_app_authorization.revoked` (delete that user's link), and a user
  linking/unlinking GitHub (the web app calls an API endpoint that enqueues
  `sync_user_acl(user_id)`).
- Beat: full ACL refresh for every repo **hourly** (mostly 304s).
- Workspace membership removal needs no job, because `app.is_member()` RLS already cuts access.

**Never-leak failure modes (fail closed):**
- No linked GitHub account → no private content. The UI says "Link GitHub to see N private repos".
- ACL older than 6 h (worker down, App suspended, API errors) → `can_read_container`
  returns false for private repos. The UI shows an "access check stale" banner. We
  accept losing availability rather than risk a leak.
- Removing access always happens before anything else (§5). A failed ACL sync never
  writes partial results, because it runs in one transaction or not at all.
- Tests: RLS leak tests for each role, covering users without links, stale ACLs,
  removed collaborators and cross-workspace access. They run in CI against real Postgres.

## 7. Local development

- **Dev App:** create "Tacit (dev)" from `infra/github/app-manifest.json` under Ahmed's
  account. Install it only on our own repos: `PhaseYon/tacit`, plus a sandbox repo
  `PhaseYon/tacit-fixtures` with seeded issues, PRs, review threads and a `docs/adr/`
  folder, made with a script using `gh`. Never install it on real private team repos.
- **Webhooks to localhost:** [smee.io](https://smee.io) (what GitHub's own docs use for
  App webhooks): set the dev App's webhook URL to a smee channel, then run
  `npx smee-client -u https://smee.io/<id> -t http://localhost:8000/webhooks/github`.
  If signature checks fail because the relayed body was re-serialized, use
  `cloudflared tunnel --url http://localhost:8000` instead. `gh webhook forward` is
  rejected because it creates repo/org webhooks, not App webhooks.
  Setup and OAuth callbacks are browser redirects, so `http://localhost:8000` works for them directly.
- **Replay:** `uv run tacit-github replay <delivery_id>` re-runs a stored
  `webhook_delivery`. The GitHub App settings UI's "Redeliver" button also works.
- **Env:** `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`,
  `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_WEBHOOK_SECRET` in
  `.env` (gitignored). The private key lives outside the repo.
- **Tests:** unit-test normalizers with `@octokit/webhooks-examples` payloads, mock the
  REST API with `respx`, and test RLS against the compose Postgres. An opt-in
  integration test (`GITHUB_IT=1`) backfills `tacit-fixtures` and checks counts, URLs and ACL rows.
- Compose: add `worker` and `beat` commands (Celery) next to `redis`.

## 8. Implementation plan

Each step is one PR-sized commit with tests.

1. **Migration 0002:** connector tables, `tacit_worker` role and grants, the restrictive
   ACL policy, `app.can_read_container` and `app.workspace_for_installation`. RLS leak tests.
2. **Worker skeleton:** Celery app (realtime and backfill queues, beat), `asyncio.run`
   wrapper, NullPool engine, worker RLS session helper. Settings for GitHub env vars.
3. **GitHub client module:** githubkit app and installation clients, Redis cache
   strategy, capped retry policy, rate-limit budget helper. App manifest file.
4. **Webhook receiver and install flow:** `/webhooks/github` (verify, dedupe, enqueue),
   `/github/connect` + `/github/setup` (state JWT, installer verification),
   `installation*` handlers, repo sync.
5. **Normalizers and hashing:** pure functions from payload or REST object to
   `SourceDocument` for each kind. Bot filter, truncation, canonical hash. Fixture tests.
6. **Backfill:** paged and checkpointed tasks for issues/PRs, comments, review comments
   and reviews. `parent_id` linking.
7. **Incremental and reconcile:** webhook handlers per event, out-of-order guard,
   tombstones, 6-hourly reconcile, hourly failed-delivery redelivery, weekly sweep.
8. **Markdown docs:** tree and blob SHA diffing, glob config, `push` handler.
9. **Permission sync:** account link grant, `sync_repo_acl` / `sync_user_acl`, ACL
   webhook events, hourly refresh, staleness banner API. End-to-end leak test with two users.
10. **Dogfood:** run on `PhaseYon/tacit` and `tacit-fixtures`. Add a `connector_stats`
    SQL view (docs, tokens and estimated extraction cost per repo), with numbers
    recorded for the step 4 go/no-go decision.

## Decisions on open questions (2026-10-24)

1. **Better Auth GitHub provider:** yes. Use the GitHub App's client ID and secret for
   GitHub sign-in and linking (no separate OAuth App). Grant `tacit_worker` SELECT on
   `account ("userId", "providerId", "accountId")` only.
2. **`tacit_worker` role:** yes. Add it to `infra/postgres/init/01-roles.sh`, the CI role
   step and the test fixtures in the same commit as migration 0002.
3. **Public repos:** readable by every workspace member, with no linked account needed.
4. **Uninstall:** hide immediately (drop `container_access` rows and tombstone the
   documents in the same transaction as the webhook), then hard-delete after 30 days.
   Reinstalling within 30 days restores rows when the ACL is resynced.
5. **Multiple installations per workspace:** the schema allows it; no UI in v1.
6. **ACL staleness cutoff: 6 h** rather than 24 h. The refresh is hourly, so 6 h means
   several missed runs, and it keeps the window where a removed collaborator can still read small.
7. Still open: check `affiliation=all` against secret teams and enterprise roles with
   a real token during plan step 9. Add team listing if it's missing.
