# Tacit

**Your team's memory, wired into every AI tool.**

Tacit syncs a team's Slack, GitHub and Notion into a permission-aware knowledge graph, so you
can ask *"why did we do X, who decided, and where's the evidence?"* from the web app, or from
Claude and Cursor over MCP.

> Status: early development. Auth, workspaces, the GitHub connector, hybrid search with
> citations, the MCP server and the knowledge graph (Graphiti, opt-in) work. Slack/Notion
> connectors and the public benchmark are next.

## Architecture (so far)

```
Browser ──► Next.js (apps/web) ── Better Auth ──► Postgres (auth tables, as tacit_auth)
                │  short-lived JWT (EdDSA)
                ▼
            FastAPI (apps/api) ── verifies JWT via /api/auth/jwks
                │  SET LOCAL app.user_id / app.workspace_id
                ▼
            Postgres with row-level security (as tacit_api)
```

- **Workspaces** are Better Auth organizations (members, roles, email invites).
- **Isolation is enforced twice**: every query filters by workspace in code, and Postgres
  RLS blocks anything a filter misses. The API's database role can't read sessions, OAuth
  tokens or signing keys at all.
- **Four DB roles**: `tacit_migrator` owns tables (Alembic), `tacit_auth` is used by
  Better Auth, `tacit_api` by FastAPI and MCP (read-only on synced content), and
  `tacit_worker` by Celery jobs (scoped to one workspace per transaction).
- **Source permissions are enforced in Postgres**: a restrictive RLS policy shows a
  synced document only if it's public or your linked GitHub account can access the repo,
  based on access data less than 6 hours old. Otherwise access is denied.

## Search

Synced documents are split into ~400-token chunks and indexed two ways: embeddings
(pgvector, HNSW) and Postgres full-text (tsvector). A query runs both and fuses them with
Reciprocal Rank Fusion. Each result is the best chunk per document, with a highlighted
snippet and a link to the exact PR, issue, comment or doc. Documents are only re-embedded
when their content hash changes.

Embeddings default to a **local** model (fastembed `bge-small-en-v1.5`, 384 dims): no API
key, no per-document cost, reproducible. Set `EMBEDDING_PROVIDER=openai` (plus
`OPENAI_API_KEY`) to use `text-embedding-3-small` at 384 dims instead. Ranking weights
and re-ranking are deliberately untuned until the benchmark (build step 7) measures them.

## Knowledge graph (opt-in)

Merged PRs with their reviews, discussed issues and docs become Graphiti episodes on
Neo4j: people, components, technologies and decisions, with facts that know when they
became true and when they were replaced. Answers can say what the *current* decision is
and what it superseded.

- **Never leaks:** one graph group per repo. A search only covers repos you can read
  right now (from Postgres), never calls Graphiti with an empty group list, and drops any
  fact Postgres can't tie to a visible source, which is also where its citation comes from.
- **Costs money, so it's off by default.** It needs `GRAPH_ENABLED=true`, an LLM key
  (`claude-haiku-5-5` by default), and a per-workspace opt-in by an admin. Private repos
  need a second opt-in, because their text goes to the LLM provider. Every call is
  recorded in `llm_usage`, and a monthly budget per workspace is a hard stop.
- Run `docker compose --profile graph up -d neo4j`. See `docs/design/graph-layer.md`.

## Benchmark

On the fictional Larkspur dataset (52 "why" questions, local embeddings, $0), **hybrid
search beats vector-only RAG** (recall@10 0.865 vs 0.750, MRR 0.545 vs 0.386). Keyword-only
scored highest (0.904), probably because the questions reuse the corpus's wording. The
graph arm hasn't run yet. Numbers, caveats and how to reproduce:
[benchmark/README.md](benchmark/README.md).

## Use it from Claude or Cursor (MCP)

Tacit is a remote MCP server at `http://localhost:8000/mcp` with OAuth 2.1. The web app is
the authorization server (dynamic client registration, PKCE, consent screen), and tokens
are bound to the MCP resource. Every tool call runs with your permissions, so the
assistant sees exactly what you can see.

```bash
# Claude Code
claude mcp add --transport http tacit http://localhost:8000/mcp
```

For Claude Desktop or Cursor, add a remote MCP server with that URL. The client opens a
browser to sign in and approve access.

Tools: `list_workspaces`, `search` (hybrid, cited), `get_document` (a source plus its
review/comment thread) and `search_facts` (time-aware graph facts with citations, when
the graph is enabled).

## Local development

Needs Docker, [uv](https://docs.astral.sh/uv/), Node 22+ and pnpm.

```bash
pnpm bootstrap    # .env + dependencies
pnpm infra:up     # Postgres (pgvector), Redis, Mailpit
pnpm db:migrate   # create tables, grants, RLS policies
pnpm dev:api      # http://localhost:8000  (in one terminal)
pnpm dev:web      # http://localhost:3000  (in another)
```

Emails (verification, invites) land in Mailpit at http://localhost:8025.

To sync GitHub, create the dev GitHub App with `pnpm github:create-app` and run
`pnpm dev:worker` and `pnpm dev:webhooks`. See [infra/github/README.md](infra/github/README.md).

```bash
pnpm test         # API tests (uses a throwaway tacit_test database)
pnpm lint && pnpm typecheck
```

## Repo layout

| Path | What |
|---|---|
| `apps/web` | Next.js 16, Better Auth, shadcn/ui |
| `apps/api` | FastAPI, SQLAlchemy (async), Alembic migrations, Celery workers |
| `apps/api/src/tacit_api/connectors/github` | GitHub connector (githubkit) |
| `docs/design` | Design docs |
| `infra/postgres` | Role setup (`pnpm db:roles` re-applies it) |
| `infra/github` | GitHub App manifest and setup guide |
| `docker-compose.yml` | Local infrastructure |

## License

Apache 2.0
