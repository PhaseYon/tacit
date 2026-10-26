# Design: Notion connector (build step 6b)

Status: implemented (v1) · Owner: Ahmed Fezan · Date: 2026-10-25

Syncs Notion pages (including database rows) into `source_document`, enforced by the same
`app.can_read_container` rule as GitHub and Slack. It mirrors the Slack connector
(`docs/design/slack-connector.md`): webhook intake → dedupe → Celery, checkpointed
backfill, reconcile, fail-closed permissions. **No MCP for bulk sync, no LLM calls in this
step.** Non-goals: comments, file contents, database schemas/views, Notion AI meeting notes,
writing to Notion.

## 1. The permission problem, and the decision

What the Notion API (version `2025-09-03`) can and can't tell us:

| Question | API answer |
|---|---|
| Which pages can this integration read? | Yes: `POST /v1/search` lists everything shared with the integration. |
| Who can read page X in Notion? | **No.** Pages have no permissions / sharing field. No endpoint lists a page's or a teamspace's members, and teamspaces aren't in the API at all. |
| Who is in the Notion workspace? | Partly: `GET /v1/users` lists members and guests, but not which is which, and not their teamspaces. |
| Who authorized this token? | Yes: the OAuth token response has `owner.user` and `workspace_id`. |
| Webhook `accessible_by` | Lists the *connection owners* (bots, and the people who own them) that can see the entity. That's integration access, not Notion page ACLs. |

So we **can't read per-user ACLs**. "Shared with the integration" only means "someone with
full access chose to give this connection access". It does **not** mean every teammate can
read the page in Notion (private pages, restricted teamspaces and guest-only pages can all
be shared with an integration).

**Decision (fail closed): a Notion connection is personal.**

1. Any Tacit workspace member may connect *their own* Notion. Notion's docs say page
   access is granted per authorizing user through the page picker, and only that user can
   share pages through their connection (`bot_id` identifies one authorization).
2. Everything synced through a connection is readable **only by the Tacit user who
   connected it**: the container's only access row is that user's.
3. A page tree becomes readable by every member of the Tacit workspace **only** when a
   workspace **admin who is also the connection's owner** marks it "visible to workspace"
   in Tacit. Narrowing (unmarking, disconnecting) is allowed for the owner or any admin.
4. A non-owner admin can't widen someone else's tree: they can't see its content, so they
   can't judge it, and letting them would let any admin publish a colleague's private
   pages. (Follow-up: owner requests, admin approves.)

Trade-offs, stated plainly:
- Out of the box, teammates don't see each other's Notion content. That's less useful
  than Slack/GitHub, but those sources expose real ACLs and Notion doesn't. Guessing would
  break "never leak", so usefulness costs one explicit click per shared tree.
- Marking applies to the **whole tree**, including pages added under it later. Notion
  lets a sub-page have narrower access than its parent; the toggle's copy warns about
  this, and the admin is attesting for the tree. The UI lists every tree and its page count.
- Two people who connect the same Notion pages get two copies (one per connection).
  Search may show both to someone who can read both; harmless, and it keeps each copy's
  ACL independent (a shared row would make one person's sync change another's visibility).
- "Visible to workspace" means Tacit workspace members, not "members of the Notion
  workspace". Notion can't prove the latter (we can't tell members from guests).

## 2. Integration and OAuth

One **public** Notion integration ("Tacit"), capabilities: *Read content*, *Read user
information without email*. Not requested: insert/update content, comments, emails.

**Connect flow:** `POST /workspaces/{id}/notion/connect` (any member) returns
`https://api.notion.com/v1/oauth/authorize?client_id&response_type=code&owner=user&redirect_uri&state`.
`state` is an HS256 JWT (key = HMAC(BETTER_AUTH_SECRET, `tacit:notion-connect-state`),
audience `tacit:notion-connect`, workspace + user + nonce, 10-minute expiry), the same
construction as Slack's. Notion's page picker lets the user choose pages. Then
`GET /notion/oauth/callback?code&state` verifies `state`, exchanges the code
(`POST /v1/oauth/token`, Basic auth, same `redirect_uri`), **requires `owner.type == "user"`**
(a workspace-owned token has no person to scope it to), stores the connection and enqueues
a full sync. A `bot_id` already linked in another Tacit workspace is refused (`bot_id` is
UNIQUE; the conflicting write fails).

## 3. Identity

Better Auth 1.7.12 ships a built-in Notion provider, but (a) `accountId` is the bare
Notion user id, which isn't scoped to a Notion workspace, so it doesn't prove which
workspace someone belongs to, and (b) **every Notion OAuth grant is an integration install**
with a page picker: "Sign in with Notion" would create a second, unused bot token in the
`account` table. So we don't use Better Auth for Notion. **The connect callback is the
verified link:** Notion returns `workspace_id` and `owner.user.id` for the person who
approved, and the signed `state` ties that to the signed-in Tacit user. We store
`notion_connection(user_id, notion_workspace_id, notion_user_id)` and
`external_identity(source='notion', external_id='<workspace_id>:<user_id>')`, the same
`team:user` style as Slack. Email is never used for matching (we don't request it).
Page authors (`created_by`) resolve to the same `ws:user` ids through `GET /v1/users`.
Cross-tool `person` merging stays a graph-layer follow-up, as for Slack.

## 4. Data model (migration 0008)

Reuses `source_container`, `source_document`, `container_access`, `external_identity`,
`webhook_delivery` (`source='notion'`).

```text
notion_connection        RLS: workspace. tacit_api: SELECT on every column EXCEPT tokens
  id, workspace_id, user_id → user (CASCADE), notion_workspace_id, notion_workspace_name,
  notion_user_id, notion_user_name, bot_id UNIQUE, access_token_encrypted,
  refresh_token_encrypted, revoked_at, created/updated_at,
  UNIQUE (workspace_id, user_id, notion_workspace_id)       -- reconnect = update
notion_tree              RLS: workspace (one per container = one page tree)
  container_id pk → source_container, workspace_id, connection_id, root_id, root_type,
  title, workspace_visible, marked_by_user_id, marked_at, UNIQUE (connection_id, root_id)
notion_page              RLS: workspace, worker-only (bookkeeping, not content)
  connection_id, page_id (pk together), workspace_id, object ('page'|'data_source'),
  database_id, parent_type, parent_id, container_id, last_edited_time, fetched_at, seen_run
notion_sync_cursor       RLS: workspace, worker-only
  connection_id pk, workspace_id, mode, next_cursor, run_id, run_started_at,
  run_max_edited, watermark, last_full_at, updated_at
```

Containers: `source='notion'`, `external_id = "<connection_id>:<root_id>"`, `name` = root
title, `url` = root page URL, **`visibility = 'private'` always**. Documents:
`external_id = "<connection_id>:<page_id>"`, kind `notion_page`.

**Trees:** a page's tree root is found by walking `parent` through pages and data sources
this connection can see. The root is the first ancestor whose parent is the workspace, a
block (pages inside columns/toggles; resolving those costs API calls) or something not
shared with us. Unknown parent → its own tree, private: fail closed. Trees are recomputed
after every sync run and webhook. A page that moves changes container, and trees left with
no pages are deactivated.

**Token storage:** the Slack AES-256-GCM code is now a shared `connectors/crypto.py`
(`TokenCipher(info)`). Notion uses HKDF info `tacit:notion-token:v1` (Slack keeps
`tacit:slack-bot-token:v1`, so existing ciphertexts still decrypt), **AAD =
`notion:<bot_id>:access|refresh`**: a token copied to another row, or the refresh token
swapped into the access column, fails to decrypt. A column-level grant omits both token
columns for `tacit_api`; only `tacit_worker` reads them. Disconnecting wipes both.

SECURITY DEFINER helpers: `app.notion_connections_for(notion_ws)` (webhook routing) and
`app.list_notion_connections()` (beat jobs), worker-only.

## 5. Access sync (no API calls)

`sync_connection_acl` rewrites `container_access` for all of a connection's trees in one
transaction: the owner (if still a workspace member), plus every workspace member for
trees marked `workspace_visible`, then sets `acl_synced_at`. It runs after every sync run,
on every toggle (immediately, from the API route), and hourly from beat, which keeps it
inside the 6-hour staleness cutoff. If it stops running, access goes stale and fails
closed. Disconnect or revoked token → containers deactivated, access deleted, documents
tombstoned in one transaction, tokens wiped.

## 6. What we ingest

| Ingest | Kind | Notes |
|---|---|---|
| Pages, database rows | `notion_page` | One document per page; child pages are their own documents |
| Data sources | bookkeeping only | Needed to walk row → database → parent page |
| Archived / trashed | tombstone | `in_trash` / `archived` true, 404, or lost access |

**Normalization** (pure, `normalize.py`): page + its block tree → markdown-ish text.
Headings (`#`, `##`, `###`), bulleted/numbered lists and to-dos with nesting, toggles
(children indented), quotes and callouts (`>`), code fences with language, equations,
tables (`a | b`), dividers, bookmarks/embeds (URL), files/images (caption + name, never
contents), synced blocks and columns (children inline), child pages/databases as
`[page: title]`. Rich text uses `plain_text`, links render as `text (url)`. Title = the
`title` property. Database rows get a `Key: value` header for stable property types
(select, status, multi-select, date, people, text, number, checkbox, URL). Formulas,
rollups and `last_edited_*` are excluded. **`content_hash`** covers kind, title, body only,
so `last_edited_time` bumps with no text change (Notion bumps it for cursor moves, property
reorders and so on) never re-embed. Citation = the page `url`. Block-level anchors
(`url#blockid`) are a follow-up, once chunks remember their block.

## 7. Sync

**Client:** the official [`notion-client`](https://github.com/ramnes/notion-sdk-py) `~=3.1`
(`AsyncClient`, httpx). It retries 429 (honouring `Retry-After`, capped at 60 s) and
GET 500/503 (2 retries, backoff). Notion's limit is ~3 requests/s per integration
connection; the `backfill` queue keeps concurrency low. A 429 that survives the SDK's
retries makes the Celery task retry later. Only `connectors/notion/client.py` imports it.
Tests use respx (httpx), so no fake server is needed. Rejected: raw httpx (pagination,
retries, error types by hand), the TS SDK (wrong runtime).

**Backfill / reconcile** (one Celery task per search page, resumable): `POST /v1/search`
sorted by `last_edited_time` descending, 100 per page. For each page we compare
`last_edited_time` (minute precision in Notion) with what's stored. A page is skipped only
if it's equal **and** we fetched more than 2 minutes after that minute. Otherwise we fetch
the block tree (`GET /v1/blocks/{id}/children`, all pages, recursing into `has_children`
up to depth 8, never into child pages), normalize and upsert. The cursor (`next_cursor`,
`run_id`, max edited time) is saved after each page.
- **Incremental** (every 2 h, and after each webhook burst): stops at the first result
  older than `watermark − 10 min`.
- **Full** (on connect, then daily): scans everything. At the end, pages not seen in this
  run are checked with `GET /v1/pages/{id}`: 404, trashed, or no access → tombstone,
  otherwise kept (a page edited mid-scan can drop out of a descending-sort cursor).
- After each run: recompute trees, sync ACL, then index.

**Webhooks** (`POST /webhooks/notion`): one subscription per integration, configured in
Notion's UI. **Verification:** Notion first POSTs `{"verification_token": …}` unsigned.
Until `NOTION_WEBHOOK_VERIFICATION_TOKEN` is set, that one body shape is stored in
`webhook_delivery` (worker-only, never logged) so the operator can read it with SQL and
paste it into Notion. After that, every request needs `X-Notion-Signature: sha256=<hex
HMAC-SHA256(raw body, verification_token)>`, constant-time compared. Without a configured
token, everything else gets 401 (fail closed). Dedupe on `notion:<event id>`.
**Replay:** Notion's signature has no timestamp, so a captured request stays valid. It's
harmless because events are only signals: handlers never trust payload content, they
re-fetch with the connection's own token, and a replayed id is deduped.

**Handlers:** route by `workspace_id` to every live connection in that Notion workspace
that has the page or appears in `accessible_by`. `page.created|content_updated|
properties_updated|moved|undeleted` → re-fetch that page with each connection's token, then
recompute trees and ACL; `page.deleted` → tombstone (the safe direction; reconcile undoes
mistakes); `database.*` / `data_source.*` → incremental reconcile; `comment.*`,
`page.locked|unlocked` → ignored. 404 / `restricted_resource` → that connection's copy is
tombstoned (access was removed).

**Tokens:** Notion now returns a `refresh_token`. On 401 we refresh once, under
`SELECT … FOR UPDATE`. If another worker already rotated the token we use theirs. If the
refresh fails, the connection is revoked (fail closed) and the UI asks the user to reconnect.

**Idempotency:** upsert on `(workspace_id, source, external_id)`, with "older
`last_edited_time` never overwrites newer" (equal is allowed, because of minute precision).
Dedupe by event id. A concurrent duplicate run is skipped if a run touched the cursor in
the last 30 minutes.

## 8. Graph layer

Not in v1. Notion trees are `private`, so they could only enter the graph behind
`include_private`, but `graph/bundles.py` only selects GitHub kinds and its renderer and
participant matching are GitHub-specific. `ELIGIBLE_ROOTS` doesn't match `notion_page`, so
nothing leaks in by accident. Follow-up: a Notion page bundle (title, properties, text,
author) as an episode, one graph group per container, behind the same private opt-in.

## 9. Local development

Create a **public** integration at notion.so/profile/integrations with redirect URI
`<API>/notion/oauth/callback` (HTTPS, so use `cloudflared tunnel --url http://localhost:8000`)
and set `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`. For webhooks, add a subscription to
`<tunnel>/webhooks/notion`, then read the token:
`SELECT payload FROM webhook_delivery WHERE event = 'verification' ORDER BY received_at DESC LIMIT 1`.
Paste it into Notion's Verify dialog and into `NOTION_WEBHOOK_VERIFICATION_TOKEN`. Use a
sandbox Notion workspace; never connect a real team's Notion to a dev instance.

## 10. Needs real credentials to confirm

1. Per-authorization isolation: two users in one Notion workspace get different `bot_id`s,
   and pages shared via the "Connections" menu land only on the sharer's own connection.
   If not, the model still holds (each copy is visible only to its connector), but a
   connector could see pages a colleague shared with "Tacit". The fix would be requiring
   `accessible_by` to include the connector's person id.
2. Whether access tokens actually expire, and the refresh-token rotation semantics.
3. `bot_id` stability on re-authorization (we upsert on workspace+user+Notion workspace).
4. `accessible_by` contents for public connections, and webhook payloads for
   `page.moved` / trashed pages under `2025-09-03`.
5. Search ordering stability under concurrent edits (the full run's unseen-page check
   covers the gap), and real throughput against the ~3 req/s limit on a 5k-page workspace.
6. `data_source.database_parent` presence on search results (used to walk the tree).
