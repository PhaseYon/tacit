# Design: Local-first Tacit

Status: draft for review · Owner: Ahmed Fezan · Date: 2026-10-26

Tacit becomes one package each teammate installs (`uvx tacit`). Sync, extraction and the
MCP server run on laptops. The only shared piece is the team's own Neo4j, which the team
hosts, not us. We host nothing.

This replaces the hosted architecture on `main` (Next.js, Better Auth, FastAPI, Celery,
Redis, Postgres). That version is tagged `v0-hosted` and stays in git history as the
reference for a future enterprise offering (§11).

---

## 1. Who this is for

- **Small, high-trust teams.** Whoever has the database password can read the team's
  whole graph. Handing that password out carefully is the team's job (shared
  responsibility). We don't build per-user permissions inside a graph.
- **Users talk to Tacit through an AI client.** Claude Desktop, Claude Code and Cursor
  call the local MCP server. Nobody needs a web UI to query the graph.
- **Zero infra cost for us.** Teams bring their own Neo4j and their own LLM key.

Non-goals for this version: hosted SaaS, per-user permissions inside a graph, web UI,
webhooks, and tamper-proof audit logs (§7 covers what we do log).

## 2. Architecture

### Before (`v0-hosted`)

```
 GitHub / Slack / Notion
        │ webhooks + backfill (app/bot tokens)
        ▼
┌──────────────────────────── WE HOST ────────────────────────────────┐
│  FastAPI ──enqueue──▶ Redis ──▶ Celery workers                      │
│   ├─ webhook routes             ├─ fetch content + ACLs             │
│   ├─ search / ask API           ├─ chunk + embed                    │
│   └─ MCP server (/mcp, OAuth)   └─ Graphiti extraction (our LLM $)  │
│         │                                │                          │
│         ▼                                ▼                          │
│  Postgres + pgvector  ◀── ACL check ──  Neo4j                       │
│   docs, chunks, ACLs, RLS, 4 roles      graph facts                 │
│                                                                     │
│  Next.js + Better Auth  (login, workspaces, OAuth server, JWTs)     │
└─────────────────────────────────────────────────────────────────────┘
        ▲ browser                           ▲ MCP over HTTPS + OAuth
     Users                          Claude / Cursor
```

### After

```
 GitHub / Slack / Notion
        ▲ poll with each user's own token
        │
┌──── EACH TEAMMATE'S LAPTOP  (uvx tacit) ─────────────┐
│  Claude / Cursor ──stdio──▶ local MCP                │
│                             ├─ search                │
│                             └─ get_document          │
│                                                      │
│  tacit sync                                          │
│   ├─ scope (which repos/channels/pages)              │
│   ├─ claim write lease, read cursors                 │
│   ├─ skip if content hash already ingested           │
│   └─ Graphiti extraction (user's own LLM key)        │
│                                                      │
│  run_query()  ◀── the only path to the DB            │
│   1. write (:Access {who, op, tool, params, at})     │
│   2. run query                                       │
│                                                      │
│  secrets in OS keychain (DB password, tokens)        │
└──────────────────────────┬───────────────────────────┘
                           │ URL + password (1 per team)
                           ▼
┌──── TEAM HOSTS (we host nothing) ────────────────────┐
│  Neo4j  ─  Aura Free  │  Docker on a VPS             │
│   graph facts + episodes (with source links)         │
│   Graphiti hybrid search (vector + keyword + graph)  │
│   scope, leases, cursors, hashes, access log         │
└──────────────────────────────────────────────────────┘
```

### What moves where

| Before (we host) | After |
|---|---|
| Next.js + Better Auth | Removed. The password is the access. |
| FastAPI + MCP over HTTP with OAuth | Local MCP over stdio (FastMCP) |
| Celery + Redis + webhooks | `tacit sync` polling on laptops |
| Postgres (pgvector, full-text, RLS) | Removed. Graphiti's hybrid search in Neo4j. |
| Our LLM bill | Each user's own key |
| Our Neo4j | The team's Neo4j |

## 3. Where the database lives

The team hosts Neo4j somewhere always on and reachable by every teammate. A laptop is
only fine for solo use.

| Option | Cost | Notes |
|---|---|---|
| **Neo4j Aura Free** (default) | $0 | 200k nodes, 400k relationships, 1 GB, 1 on-demand backup. Pauses after 3 days idle and is deleted after 30 days idle (per Neo4j's FAQ). Fine to start. |
| Aura paid tier | From roughly $65/month | Same URL swap. |
| Docker on a VPS | About $5/month | We ship `deploy/neo4j/docker-compose.yml` (Neo4j 5.26, TLS, volume, nightly dump). |
| Docker on a laptop | $0 | Solo use only. |

Sources: [Aura quotas and limits](https://neo4j.com/docs/aura/quotas-and-limits/),
[Aura Free](https://neo4j.com/free-graph-database/).

**Idle pause:** `tacit sync` runs regularly, so an active team never hits the 3-day pause.
`tacit doctor` warns if the instance is paused.

## 4. One graph per team, one Graphiti group

In `v0-hosted` each repo, channel or page tree had its own Graphiti group, so access
could be checked per container. Without per-user permissions that's not needed, and it
hurts:

- **Dedup and context only work inside a group** (graph-layer §0). "Dana" in Slack and
  "dana-gh" on GitHub can only merge into one person if they're in the same group.
- **Neo4j full-text search returns nothing once query words + groups ≥ 128**
  (graph-layer §0). That limit would hit a team with many channels.

So **every graph uses a single group** (`tacit`). Graphiti requires episodes in a group to
be added one at a time, in order, so all extraction writes go through **one write lease
per graph** (§6). Fetching from the sources can still run in parallel. For a small team,
one writer at a time is plenty.

## 5. What's in Neo4j

```
Neo4j
 ├─ Graphiti data     :Entity, :Episodic, :Community, edges  ◀── search reads only these
 └─ Tacit metadata    :TacitScope, :TacitLease, :TacitCursor,
                      :TacitDoc (content hash), :TacitPerson,
                      :TacitAccess, :TacitMeta (schema version)
```

- Metadata labels start with `Tacit` and are never returned by search.
- `:TacitMeta {schema_version}`: `tacit` refuses to run against a newer schema and
  migrates an older one on `tacit init`.
- Uniqueness constraints on `TacitLease.name`, `TacitCursor.source`,
  `TacitDoc.external_id`, `TacitPerson.key`.

### 5.1 Episodes and citations

- One source item (PR, issue, Slack thread, Notion page) becomes one or more episodes.
  Long items, mostly Notion pages, are split at headings so each episode stays a
  reasonable size.
- Each episode stores `source_description = "<source>:<external_id>"` and the URL in the
  episode name or attributes. Every fact links to the episodes it came from, so every
  answer can cite the exact PR, message or page.
- `:TacitDoc {external_id, url, title, content_hash, episode_uuids, updated_at}` records
  what was ingested. It drives the hash skip and `get_document`.

### 5.2 One identity per person

`:TacitPerson {key, name, github, slack, notion}` maps accounts across tools. It's filled
automatically where emails match and by hand with `tacit people link`. The mapping is
passed to extraction so Graphiti resolves "Dana", "@dana" and "dana-gh" to one entity.
This reuses the matching logic from `graph/pipeline.py`.

## 6. Sync

```
tacit sync   (manual, or every N minutes via `tacit sync --watch` / cron / launchd)
  │
  ├─ 1. read :TacitScope            which repos / channels / pages are in this graph
  ├─ 2. for each source in scope (parallel, with the user's own token):
  │       read :TacitCursor ─▶ fetch items changed since cursor
  │       skip items whose content_hash matches :TacitDoc            (no LLM spend)
  │       queue the rest
  ├─ 3. claim write lease (:TacitLease {name:"writer"}, 10 min, renewed while working)
  │       held by someone else? ─▶ leave items queued, exit cleanly, retry next run
  ├─ 4. for each queued item, oldest first:
  │       run_query(write) ─▶ Graphiti add_episode ─▶ upsert :TacitDoc
  │       advance :TacitCursor only after the episode commits
  └─ 5. release lease
```

### 6.1 Why the write lease

```
WITHOUT a lease
 t=0  Alice polls #eng ──▶ sees msg "Dana picked Redis"
 t=0  Bob   polls #eng ──▶ sees the same msg
 t=1  Alice: Graphiti "is there a Dana node?" ─▶ no
 t=1  Bob:   Graphiti "is there a Dana node?" ─▶ no
 t=2  Alice: CREATE (Dana)                     ┐
 t=2  Bob:   CREATE (Dana)                     ┘─▶ 2 Dana nodes, LLM paid twice

WITH a lease
 Alice: claim lease ─▶ got it ─▶ ingest ─▶ advance cursor ─▶ release
 Bob:   claim lease ─▶ held by Alice, skip
 later, Bob: claim ─▶ got it ─▶ resumes from Alice's cursor (no redo)
```

Claiming is one Cypher statement in a write transaction:
`MERGE (l:TacitLease {name:'writer'})` then set holder and expiry only if the lease is
free, expired, or already ours. The uniqueness constraint plus Neo4j's node write lock
make it atomic. If a laptop dies mid-run, the lease expires and the next run resumes from
the last committed cursor.

### 6.2 Sync scope

Scope is the permission boundary. Anything outside it is never fetched.

- Stored in Neo4j (`:TacitScope`), so the whole team shares one definition, and changes
  go through `run_query()` and get logged.
- Edited with `tacit scope add github:acme/api`, `tacit scope add slack:#eng`,
  `tacit scope add notion:<page-id>` (the whole subtree), and `tacit scope ls/rm`.
- Defaults are conservative:
  - **Slack:** public channels only. Private channels need `--private` and print a
    warning that everyone with the password will be able to read them. DMs and group
    DMs are never syncable.
  - **GitHub:** only repos listed explicitly.
  - **Notion:** only listed pages and their subtrees.
- A user can only sync what their own token can read. If Alice adds `slack:#eng` but
  isn't in it, her runs skip it and someone who is in it picks it up.

### 6.3 Source credentials

Each user authenticates as themselves. Tokens are stored in the OS keychain (`keyring`).

| Source | Credential | Setup |
|---|---|---|
| GitHub | Fine-grained PAT, or reuse `gh auth token` | None |
| Slack | User token (`xoxp-`) from the team's own Slack app | One teammate creates the app once (we ship a manifest in `infra/slack`); each user installs it and pastes the token. An app built for the team's own workspace avoids the history limits Slack applies to apps distributed outside the Marketplace. |
| Notion | Internal integration token | One teammate creates the integration and shares the scoped pages with it; the token is shared with the team the same way as the DB password. |

The existing clients in `connectors/{github,slack,notion}` are reused. They switch from
app, bot and OAuth tokens to these user tokens. The webhook handlers go away.

### 6.4 Cost

Every new or changed item costs Graphiti LLM calls (roughly 6–10 per episode,
graph-layer §0), paid with the syncing user's own key. Controls:

- The content-hash skip means each version of an item is paid for once, by whoever
  ingests it first.
- The cost ledger and budget stop from `graph/costs.py` stay. `tacit sync` shows
  estimated and actual spend per run, and `--max-usd` caps a run.
- The existing quality gate (skip bot noise, tiny messages, CI chatter) stays in front
  of extraction.

## 7. Access log

Every read and write goes through one function, `run_query()`. The LLM never writes
Cypher. MCP tools build fixed queries from their parameters.

```
LLM ─▶ search(query="why redis?", limit=10)
         │
         ▼
       tool code builds fixed Cypher + params
         │
         ▼
       run_query(who, op="read", tool="search", cypher, params)
         ├─ 1. log (:TacitAccess {who, op, tool, params, result_count, at, client})
         └─ 2. run on Neo4j ─▶ results ─▶ LLM
```

- **Log first, fail closed.** If the log write fails, the query does not run.
- **Who:** the identity from `tacit init` (email). It's self-asserted, which fits the
  shared-responsibility model.
- **Writes too.** Sync writes are logged per episode, so every fact can be traced back
  to the person whose run ingested it.
- **Retention:** entries older than 90 days (configurable) are deleted at the end of a
  sync.
- **Reading it:** `tacit log --who alice@ --since 7d --grep redis`. This doubles as the
  root-cause query.
- **Known gaps, documented, not engineered around:**
  - Anyone with the password can query Neo4j directly (Browser, `cypher-shell`), skip
    the log, or delete log entries.
  - Closing that gap needs per-person DB logins or a gateway (§11).

## 8. Search and MCP

### 8.1 Tools (local FastMCP over stdio)

| Tool | What it does |
|---|---|
| `search(query, limit, as_of?)` | Graphiti hybrid search (vector + BM25 + graph, RRF). Returns current facts plus superseded ones marked with their validity window, each with source citations. |
| `get_document(url_or_id)` | Full stored text of a cited item from its episodes, with its link. |
| `status()` | Graph name, last sync time per source, item counts. Helps the LLM say how fresh its answer is. |

No `ask` tool: in MCP the client is already the LLM, so it calls `search` and answers
itself. `tacit ask "..."` stays as a CLI command (LangGraph retrieve → answer → validate
citations) for terminal use.

Config for clients is printed by `tacit mcp install --client claude|cursor|claude-code`:

```json
{ "mcpServers": { "tacit": { "command": "uvx", "args": ["tacit", "mcp"] } } }
```

### 8.2 Embeddings

Local fastembed (bge-small, 384 dimensions) through the existing Graphiti embedder
adapter, so search needs no API key. Everyone on a team must use the same embedder;
`:TacitMeta` stores the model name and dimension, and `tacit` refuses to mix them.

### 8.3 What we lose: the Postgres keyword arm

In the published benchmark (`benchmark/results/2026-10-25-fastembed.md`),
Postgres keyword-only search beat our hybrid (recall@10 0.904 vs 0.865). Graphiti's
search has its own BM25 arm over Neo4j full-text indexes, but on different text (facts
and episode content, not chunks). We re-run the benchmark on the new stack (§10) and
publish the result whichever way it goes. If episode keyword search turns out clearly
worse, the fix is a full-text index on episode content inside Neo4j, not bringing
Postgres back.

## 9. CLI

```
tacit init                 connect to Neo4j, set identity, create constraints/schema
tacit auth github|slack|notion
tacit scope add|ls|rm
tacit sync [--watch 15m] [--max-usd 2]
tacit people ls|link
tacit search "why redis?"
tacit ask "why did we pick redis?"
tacit log [--who] [--since] [--grep]
tacit mcp                  run the MCP server (stdio)
tacit mcp install --client claude|cursor|claude-code
tacit doctor               check DB reachability, pause state, tokens, schema
```

Built with Typer. Config (graph URL, identity, embedder) in
`~/.config/tacit/config.toml`; secrets in the keychain only.

## 10. Benchmark and demo

- The demo generator stays. `tacit demo load` writes the fictional startup into a local
  Docker Neo4j.
- The benchmark arms become: Graphiti hybrid (default recipe), Graphiti vector-only,
  Graphiti keyword-only, plus cost per ingested item and query latency.
- It runs with the replay LLM provider where possible so numbers are reproducible, and
  the 2026-10-25 Postgres results stay published as the `v0-hosted` baseline.
- The open caveat stays: the same author wrote the questions and the corpus, so a
  held-out paraphrased question set is still needed.

## 11. Later: hosted / enterprise

Not built now. Recorded so the local design doesn't block it.

```
 laptops (same tacit CLI) ── per-user API key ──▶ Gateway ──▶ Neo4j
                                                   1. key → user + team
                                                   2. access log (can't be skipped)
                                                   3. team scoping
```

- `run_query()` is the seam. In hosted mode it sends the query to the gateway instead of
  Neo4j, and nothing else on the laptop changes.
- Small scale: one Neo4j per team (physical isolation). Large scale: one shared Neo4j
  with the gateway adding the team filter. `v0-hosted`'s auth and RLS work is the
  starting point.

## 12. Repo changes

### Delete

| Path / piece | Why |
|---|---|
| `apps/web` (Next.js, Better Auth, shadcn) | No hosted UI or login |
| FastAPI app, routes, `deps.py`, `auth.py` (JWKS) | No server |
| `mcp_server.py` HTTP/OAuth setup | Replaced by stdio |
| `worker.py`, Celery tasks, Redis | Polling on laptops |
| Webhook handlers and routes for all three sources | Replaced by polling |
| Postgres: `db.py`, Alembic migrations 0001–0008, `migrations/rls.py`, `search/{indexer,hybrid,routes,tasks}.py` | Graphiti search in Neo4j |
| `infra/postgres`, Mailpit, Redis in `docker-compose.yml` | Not used |
| `graph/access.py` (Postgres post-check) | No per-user filtering |
| Root pnpm workspace and scripts | No Node app left |

### Keep, reworked

| Piece | Change |
|---|---|
| `connectors/{github,slack,notion}/{client,normalize,backfill}.py` | User tokens; return items instead of writing to Postgres |
| `graph/{adapters,costs,ontology,bundles,pipeline}.py` | Write to Neo4j metadata instead of Postgres tables; single group |
| `llm/*`, `agent/ask.py` | Unchanged; ask retrieves from Graphiti |
| `search/{embeddings,chunking}.py` | Embedder kept; chunking reused to split long pages |
| `demo/`, `benchmark/` | Load into Neo4j; new arms |
| Tests | Rewritten against a Docker Neo4j; fake LLM and API mocks kept |

### Add

- Package `tacit` (Python 3.13, uv), CLI entry point `tacit`. Layout: move `apps/api` to
  `src/tacit` at the repo root.
- `store/` (Neo4j metadata, `run_query()`, leases, cursors, schema migrations),
  `sync/` (orchestrator), `cli/`, `mcp/` (stdio server).
- `deploy/neo4j/docker-compose.yml` for teams self-hosting on a VPS.
- `docs/quickstart.md`: Aura Free → `uvx tacit init` → `scope add` → `sync` → `mcp install`.

## 13. Plan (each step is its own small commits and PR)

1. Tag `v0-hosted`. Restructure into the `tacit` package; delete the web app and server
   pieces listed above.
2. `store/`: Neo4j schema, `run_query()` with the access log, leases, cursors, hash skip.
   Tests against Docker Neo4j.
3. `tacit init`, `auth`, `scope`, `doctor`, keychain secrets.
4. GitHub sync end to end (poll → hash skip → lease → Graphiti) on our own repos.
5. Local MCP (`search`, `get_document`, `status`) and `mcp install`. Try it in Claude
   Desktop and Cursor.
6. Slack and Notion sync.
7. People linking, `tacit ask`, `tacit log`.
8. Demo load and benchmark re-run; publish results.

## 14. Open questions

1. **Cheap mode.** Every item costs extraction LLM calls. Should there be a mode that
   stores items as searchable episodes with embeddings and full-text but no extraction,
   and only extracts selected sources? It would cut cost a lot, at the price of fewer
   graph facts.
2. **Slack token handling.** Pasting a user token is the simplest flow. A local OAuth
   loopback flow is nicer, but Slack's redirect-URL rules for localhost need checking.
3. **Notion token.** One shared integration token versus per-user OAuth. Shared is
   simpler and matches the shared-password model.
4. **CLAUDE.md goals.** Per-user permission-aware retrieval and OAuth MCP were headline
   features. This design replaces them with scope-as-boundary plus an access log.
   CLAUDE.md should be updated to match once this doc is approved.
