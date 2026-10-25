# Tacit

**Your team's memory, wired into every AI tool.**

Tacit syncs a team's Slack, GitHub and Notion into a permission-aware knowledge graph, so you
can ask *"why did we do X, who decided, and where's the evidence?"* from the web app, or from
Claude and Cursor over MCP.

> Status: early development. Auth, workspaces, invites and the GitHub connector work;
> search and the graph layer are next.

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
