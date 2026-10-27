# Tacit website

The public one-page site for Tacit: pitch, hands-on example, how it works, benchmark
numbers, trust model and quickstart. Spec: [`docs/design/local-first.md` §14](../docs/design/local-first.md#14-website).

Next.js 16 (App Router, Cache Components), TypeScript, Tailwind v4. Its own pnpm project
(own `package.json` and lockfile). No analytics, cookies or tracking.

## Develop

```bash
cd site
pnpm install
pnpm dev            # http://localhost:3000
pnpm lint
pnpm typecheck
pnpm build          # regenerates data/benchmark.json first
```

Everything works without env vars. The demo box then shows recorded example results,
and stats that can't be fetched show a dash.

## Environment variables

All optional, all server-side only (none are `NEXT_PUBLIC_`, so none reach the browser).

| Variable | Used for |
|---|---|
| `DEMO_NEO4J_URI` | Demo database, e.g. `neo4j+s://xxxx.databases.neo4j.io` |
| `DEMO_NEO4J_USER` | Demo database user (usually `neo4j`) |
| `DEMO_NEO4J_PASSWORD` | Demo database password |
| `DEMO_NEO4J_DATABASE` | Database name, if not the default |
| `GITHUB_TOKEN` | Raises the GitHub API rate limit for the stats (no scopes needed) |
| `PYPI_PACKAGE` | PyPI name to show downloads for (default `tacit`) |
| `SITE_URL` | Canonical URL for metadata (default `http://localhost:3000`) |

## What's on the page and where it comes from

| Section | Source | Rendering |
|---|---|---|
| Stars, contributors, latest release | GitHub REST API | `'use cache'` + `cacheLife('hours')`: rebuilt in the background at most hourly |
| PyPI downloads | pypistats.org | Same. Only shown when the PyPI project's URLs point at `github.com/PhaseYon/tacit` |
| Benchmark numbers | `data/benchmark.json` | Static, at build |
| Demo search | Demo Neo4j | Dynamic route handler |

API failures never fail the build: each fetch is best-effort and failures are cached
for minutes instead of hours.

**PyPI name.** `tacit` on PyPI currently belongs to an unrelated project. Until ours is
published (under that name or another, set via `PYPI_PACKAGE`), the site shows "soon" for
downloads and adds an install-from-GitHub command under the hero
(`uvx --from git+https://github.com/PhaseYon/tacit tacit init`). Both go away on their own
once the PyPI project links to this repo.

### Benchmark data

`scripts/benchmark-data.mjs` (run by `pnpm build`, or `pnpm data`) reads
`../benchmark/results/*.json` and writes `data/benchmark.json`. It copies measured values
only and never invents numbers:

- Files without a `stack` field are from the old hosted stack. The newest is shown as the
  **v0 (Postgres) baseline**.
- Files with `"stack": "local-first"` fill the new-stack table (arms `chunk_hybrid`,
  `chunk_vector`, `chunk_keyword`, `graph`). Arms that are missing show **pending**.

If `../benchmark` isn't there (a deploy that only uploads `site/`), the committed
`data/benchmark.json` is used as is, so commit it after re-running the script.

## Hands-on example

`POST /api/demo-search` with `{"id": "elasticsearch"}` (or `GET ?id=...`, used by the
keep-alive). Only the preset questions in `lib/demo-questions.ts` are accepted; visitors
can't send their own text.

- Runs one fixed, parameterized keyword query on each preset's key terms and returns the
  top 3 distinct results (an ADR and the PR that added it count as one).
- Searches the same chunk index as the CLI: full-text index `tacit_chunk_text` on
  `:TacitChunk(text, title)`, joined to `:TacitDoc` via `HAS_CHUNK`, best chunk per doc.
  Returns title, url, source, kind, repo, date and a snippet around the first match.
- **Keyword only.** Vector search would need the query embedded with the same
  bge-small-en-v1.5 model in Node (ONNX runtime + model), which is too heavy for a
  serverless function. The UI says so. (On the v0 benchmark, keyword-only scored highest
  anyway.)
- Read-only session, 5 s query timeout, 200-character cap, 4 KB body cap, per-IP token
  bucket (10 burst, 10/min) held in memory per instance.
- If the env vars are missing or the query fails, it returns recorded **example results**
  from `data/demo-examples.json` (snippets copied from `demo/data`), labeled as such in
  the UI. It never shows an error for a down database.
- Unlike the CLI, the demo route does not write to the access log: public traffic would
  fill the free tier's node limit.

### Demo database

1. Create a Neo4j Aura Free instance.
2. Load the fictional Larkspur Labs data with the CLI (`tacit demo load`, design §10)
   pointed at it.
3. Set the three `DEMO_NEO4J_*` variables in Vercel.

Aura Free pauses after 3 idle days. The workflow
[`.github/workflows/demo-keepalive.yml`](../.github/workflows/demo-keepalive.yml) queries
the route daily. Set the repository variable `DEMO_SITE_URL` (Settings → Secrets and
variables → Actions → Variables) to the deployed URL to turn it on. It fails if the
route served example results, since that means Neo4j wasn't reached.

## Deploy to Vercel

1. Import the repo in Vercel and set **Root Directory** to `site`. Keep "Include files
   outside the root directory" on (the default), so the build can read `../benchmark`.
2. Framework preset: Next.js. Install and build commands are the defaults
   (`pnpm install`, `pnpm build`).
3. Add the environment variables above (at least the three `DEMO_NEO4J_*` ones, or the
   example falls back to recorded results).
4. Deploy, then set `DEMO_SITE_URL` in GitHub for the keep-alive.

Nothing here is Vercel-specific: `pnpm build && pnpm start` runs anywhere Node 20+ runs.

## TODO

- The numbers section reads `benchmark/results` and `benchmark/extraction`; re-run those
  and rebuild to update it.
- Add `deploy/neo4j/docker-compose.yml` to the repo (the hero links to it).
