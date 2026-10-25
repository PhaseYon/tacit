# Design: Slack connector (build step 6a)

Status: implemented (v1) · Owner: Ahmed Fezan · Date: 2026-10-25

Syncs channel messages and threads into `source_document`, with a per-channel ACL that
Postgres enforces through the same `app.can_read_container` rule as GitHub. It mirrors the
GitHub connector (`docs/design/github-connector.md`): webhook intake → dedupe → Celery,
checkpointed backfill, reconcile, fail-closed permission sync. **No MCP for bulk sync, no
LLM calls in this step.** Non-goals: DMs and group DMs, Slack Connect shared channels
from other orgs, Enterprise Grid org-wide installs, file contents, huddles/canvases.

## 1. Slack app and scopes

**One Slack app ("Tacit")** does both jobs: bot install for sync, and "Sign in with Slack"
(OpenID Connect) for identity linking. The manifest is `infra/slack/app-manifest.json`.

| Bot scope (read-only) | Why |
|---|---|
| `channels:history`, `groups:history` | Messages and threads in public / private channels **the bot was invited to** |
| `channels:read`, `groups:read` | Channel list, names, archive state, and `conversations.members` for ACLs |
| `users:read` | Directory: names for mention rendering, bot/guest/deactivated flags for ACLs |
| `team:read` | Team domain, for citation permalinks |

**Not requested:** `im:*` / `mpim:*` (DMs are private conversations; out of scope by default —
adding them later needs its own opt-in and per-user consent), `users:read.email` (we never
match identities by email, so we don't collect it), `chat:write` (read-only product),
`channels:join` (the bot never joins channels on its own).

**Channel selection is opt-in by invite:** Tacit syncs exactly the channels where someone
ran `/invite @Tacit`. A team decides channel by channel what Tacit sees, which is the
right default for chat (much more personal than PRs). Private channels work the same way
and stay private through the ACL (§5).

User (OIDC) scopes: `openid profile email`. Bot events: `message.channels`,
`message.groups`, `member_joined_channel`, `member_left_channel`, `channel_rename`,
`channel_archive`, `channel_unarchive`, `channel_deleted`, `channel_left`, `group_rename`,
`group_archive`, `group_unarchive`, `group_deleted`, `group_left`, `team_join`,
`user_change`, `app_uninstalled`, `tokens_revoked`. Token rotation: off in v1 (bot tokens
don't expire); turning it on later needs a refresh job.

## 2. Libraries

**[`slack_sdk`](https://github.com/slackapi/python-slack-sdk) `~=3.45`**, the official SDK:
`AsyncWebClient` (Web API, cursor pagination, typed errors),
`slack_sdk.signature.SignatureVerifier` (v0 HMAC-SHA256, constant-time compare, rejects
timestamps more than 5 minutes off), and the built-in `AsyncRateLimitErrorRetryHandler`
(honours `Retry-After` on HTTP 429). We subclass the handler to **cap in-process waits at
60 s**; longer waits raise and the Celery task retries with a countdown (same policy as
GitHub). Only `connectors/slack/client.py` imports `slack_sdk`.

| Rejected | Why |
|---|---|
| `slack_bolt` | A whole app framework with its own OAuth flow and installation store. We already have routes, workers and an encrypted token store; we need three pieces of the SDK, not a second web framework. |
| Raw `httpx` | Signature checks, pagination, rate-limit retries all become custom code. |

The async client runs on `aiohttp`, so tests mock `https://slack.com/api/*` with
**`aioresponses`** (respx only intercepts httpx). Same rule: no network in tests.

## 3. Install flow (OAuth v2)

1. A Tacit workspace **admin** with a **linked Slack account** (§6) clicks "Connect Slack".
   `POST /workspaces/{id}/slack/connect` returns
   `https://slack.com/oauth/v2/authorize?client_id&scope=<bot scopes>&redirect_uri&state`.
   `state` is a JWT (HS256, key = HMAC(BETTER_AUTH_SECRET, "tacit:slack-install-state"),
   audience `tacit:slack-install`, workspace + user + nonce, 10-minute expiry) — the same
   construction as GitHub's.
2. Slack redirects to `GET /slack/oauth/callback?code&state`. We verify `state`, exchange
   the code with `oauth.v2.access` (client secret + the same `redirect_uri`), then require
   **`authed_user.id` in team `team.id` to equal the Tacit user's OAuth-verified Slack link**.
   That proves the person who approved the install in Slack is the Tacit admin who started
   it. Who may install apps into the Slack team is Slack's own policy (admins can require
   approval); we don't duplicate it, because visibility is decided per user by the ACL,
   not by who installed.
3. `is_enterprise_install` → refused (Grid org installs out of scope). A team already
   linked to another Tacit workspace → refused (`team_id` is UNIQUE; the conflicting insert
   fails under RLS, exactly like GitHub installations).
4. Store the installation with the **encrypted** bot token (§4), call `team.info` for the
   domain, then enqueue `sync_team` (channels → ACL → backfill).

## 4. Data model (migration 0007)

Reuses `source_container`, `source_document`, `container_access`, `external_identity`,
`webhook_delivery` (`source='slack'`; adds `webhook_delivery.team_id`). Slack ids are
stored as **`{team_id}:{id}`** everywhere (Slack only guarantees ids unique per team).

```text
slack_installation          RLS: workspace. tacit_api: SELECT on every column EXCEPT the token
  id, workspace_id, team_id UNIQUE, team_name, team_domain, enterprise_id, app_id,
  bot_user_id, scopes text[], bot_token_encrypted bytea NOT NULL,
  installed_by_user_id → user, installed_by_slack_user_id, revoked_at, created/updated_at
slack_channel               RLS: workspace
  container_id pk → source_container, workspace_id, team_id → slack_installation,
  channel_id, name, is_private, is_archived, sync_enabled, backfill_status,
  last_reconciled_at, UNIQUE (workspace_id, team_id, channel_id)
slack_sync_cursor           RLS: workspace, worker-only
  container_id, resource ('history'), oldest_ts (watermark), next_cursor, run_latest_ts
```

Containers: `external_id = "T:C"`, `name = "#general"`, `url =
https://<domain>.slack.com/archives/<C>`, `visibility = 'private' | 'internal'` (§5).

**Bot token storage:** AES-256-GCM (`cryptography`), key = HKDF-SHA256(BETTER_AUTH_SECRET,
info `tacit:slack-bot-token:v1`), random 96-bit nonce, **AAD = team_id** (a ciphertext
copied onto another team's row won't decrypt). Stored as `0x01 || nonce || ciphertext`
(leading key-version byte for future rotation). `tacit_api` has a column-level grant that
omits `bot_token_encrypted`, so the API role can't even read the ciphertext; only
`tacit_worker` can. Rotating BETTER_AUTH_SECRET means reconnecting Slack (documented).

SECURITY DEFINER helpers (like GitHub's): `app.workspace_for_slack_team(text)` for webhook
routing and `app.list_slack_installations()` for beat jobs.

## 5. Permission model (never leak)

**Rule: a Tacit user reads a channel's messages only if (a) they're a member of the Tacit
workspace, (b) their Slack account is linked through OAuth to this Slack team, (c) Slack
says that account can read the channel, and (d) the ACL is under 6 hours old.**

| Channel | Slack users allowed (then ∩ verified links ∩ workspace members) |
|---|---|
| Private | `conversations.members` of the channel |
| Public | channel members **∪** full members of the team (not guests) |

Always excluded: deactivated users, bots, Slackbot. Multi- and single-channel guests can
only read the channels they're in, so they get public channels only via membership.

**Why public channels are not `visibility='public'`:** GitHub's `public` means "on the
internet", so everyone in the Tacit workspace may read it. A public Slack channel is
public only *inside* that Slack team. A Tacit member who isn't in the Slack team (a
contractor invited for GitHub only, or someone whose Slack account was deactivated) must
not see it. So public channels are stored as `internal`, which `can_read_container`
treats like private: an access row is required. Unlinked users see no Slack content;
the UI says "Link Slack to see N channels". Fail closed.

**Sync (`sync_team_acl`)**: fetch `users.list` (also refreshes `external_identity`, used to
render mentions), then `conversations.members` for every active channel. **All API calls
happen first; then one transaction** replaces `container_access` for every channel and
sets `acl_synced_at`. Any API error → nothing written; access goes stale after 6 h.

**Freshness:** hourly beat refresh; events trigger a resync: `member_joined_channel`,
`team_join`, `user_change` (deactivation, guest changes). `member_left_channel` on any
channel **deletes that user's row immediately** (no API call) before the resync.
`channel_deleted`, bot removed (`channel_left`/`group_left`), `app_uninstalled`,
`tokens_revoked` → container(s) deactivated, access rows deleted, docs tombstoned in one
transaction. Archived channels stay readable (as in Slack) but stop syncing new messages.

## 6. Identity linking

Better Auth 1.7 ships a built-in Slack provider, but it sets `account.accountId` to the
bare Slack user id, and the team id lands only in the stored id token, a column no service
role can read. So we register Slack through Better Auth's **`genericOAuth` plugin**
(provider id `slack`, Slack's OIDC endpoints) with
`accountSubject = "<team_id>:<user_id>"` taken from the `openid.connect.userInfo`
claims. The link comes from an OAuth round trip from a signed-in session, so it's proven.
Email is never used for matching (and not used to merge with GitHub identities either).
Users link with `authClient.linkSocial({ provider: "slack" })`; workers read only
`account(userId, providerId, accountId)` (existing column grant). Linking Slack also
lets someone **sign in** with Slack. A person in two Slack teams links twice (one
account row per team).

`external_identity(source='slack', external_id='T:U', login=<Slack handle>)`. Cross-tool
`person` merging (Slack ↔ GitHub) happens only through a Tacit user who has verified both
links — a follow-up in the graph layer; v1 never merges by name or email.

## 7. What we ingest

| Ingest | Kind | Notes |
|---|---|---|
| Top-level messages (incl. thread roots) | `slack_message` | `parent_id` NULL |
| Thread replies (incl. `thread_broadcast`) | `slack_reply` | `parent_id` = the thread root's document |
| Edits | same row | `message_changed`; out-of-order guard on `edited.ts` |
| Deletes | tombstone | `message_deleted`; `message_changed` to subtype `tombstone` |
| File shares | text only | file *names and types* in metadata; no file contents |

**Skipped:** bot messages (`bot_id`, `subtype=bot_message`, bot users), joins/leaves,
topic/purpose/name changes, pins, channel system subtypes, `ekm_access_denied`, empty
messages. One document per message, so a citation points at the exact message:
`https://<domain>.slack.com/archives/<C>/p<ts without dot>` (replies add
`?thread_ts=<root>&cid=<C>`). Constructed, not `chat.getPermalink` (one API call per
message for the same URL).

**Normalization** (pure, `normalize.py`): Slack mrkdwn → plain text: `<@U1>` → `@handle`
via the directory map, `<#C1|name>` → `#name`, `<!here>` → `@here`,
`<!subteam^S1|@eng>` → `@eng`, `<url|label>` → `label (url)`, `<!date^…|fallback>` →
fallback, then unescape `&lt; &gt; &amp;`. Title: root's first line (≤ 120 chars); replies
inherit the root's title in search. **`content_hash`** covers kind, title, body and file
names. Reactions, `edited`, `reply_count`, `latest_reply`, read markers are excluded, so
emoji noise never triggers re-embedding.

## 8. Sync

**Events API** (`POST /webhooks/slack`): raw body → `SignatureVerifier.is_valid` (401 on
failure or a timestamp > 5 min old) → `url_verification` returns the challenge →
`INSERT … ON CONFLICT DO NOTHING` into `webhook_delivery` with `delivery_id =
"slack:" + event_id` → enqueue → 200. **Replay protection:** an old captured request
fails the 5-minute window; a replay inside it (and Slack's own retries, which reuse
`event_id`) hits the dedupe row. No Slack calls in the request path (Slack's 3 s timeout).

**Handlers** normalize message events straight from the payload (no API call), resolving
mentions from `external_identity`. Channel events update `slack_channel` /
`source_container` and enqueue sync work. Unknown team → `ignored`.

**Backfill** (one Celery task per page, resumable): `users.conversations` (bot's channels)
→ for each channel `conversations.history(oldest=watermark, limit=200, cursor)`; for
every thread root in the page whose `latest_reply` differs from what's stored,
`conversations.replies` (all pages). Checkpoint `next_cursor` + the max `ts` seen; after
the last page the watermark moves to it. Horizon: `SLACK_BACKFILL_DAYS` (default 365) to
bound cost on large teams. Rate limits: history/replies are Tier 3 (~50/min);
the `backfill` queue keeps concurrency low, and 429s retry via `Retry-After`.

**Reconcile** (every 6 h): re-list history from `watermark − 2 days` (catches missed
messages and replies to recent threads; upserts with unchanged hashes cost nothing
downstream). **Known gap:** a missed webhook for a reply to a thread older than 2 days,
and deletions while webhooks were down, are not caught in v1 (no "changed since" API);
follow-up: weekly deep sweep of the last 30 days that also tombstones missing messages.

**Idempotency:** dedupe by `event_id`; upsert on `(workspace_id, source, external_id)`
with external id `T:C:ts`; `source_updated_at = edited.ts or ts` with the same
"older never overwrites newer" guard as GitHub; orphan replies relinked after each page.

## 9. Graph layer

Threads become graph episodes only through the existing gate: Slack channels are
`internal`, so they count as private and need the workspace's `include_private` opt-in.
v1 **does not** add Slack threads to `graph/bundles.py`: the renderer, ontology prompt
and participant linking (`_participants` matches `source='github'` logins) are GitHub
specific, and matching Slack handles against GitHub logins would merge people by name,
which §6 forbids. Follow-up: a `slack_message` root with ≥ 2 human replies becomes a
bundle, rendered `@handle (date): text`, participants resolved against
`external_identity(source='slack')`.

## 10. Local development

Create the app from `infra/slack/app-manifest.json` at api.slack.com/apps ("From a
manifest"), install to a **sandbox Slack workspace** only, and set `SLACK_CLIENT_ID`,
`SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET` (API + web). Events need a public URL:
`cloudflared tunnel --url http://localhost:8000` and set the Event Subscriptions URL to
`<tunnel>/webhooks/slack`. OAuth redirects (`/slack/oauth/callback`, Better Auth's
`/api/auth/callback/slack`) must be HTTPS for Slack, so dev uses the tunnel for
both. Never connect a real team's Slack to a dev instance.

## 11. Open items (need a real Slack workspace)

1. Confirm `conversations.members` works on archived private channels with a bot token
   (fallback: freeze the last ACL, which goes stale and fails closed).
2. Confirm the OIDC `userInfo` claims (`https://slack.com/team_id`, `…/user_id`) for a
   multi-workspace user; Grid users may need one link per workspace.
3. Measure Tier-3 backfill throughput on a 100k-message channel; tune page size/horizon.
