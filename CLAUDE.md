# Tacit

**Your team's memory, wired into every AI tool.**

Tacit syncs a team's Slack, GitHub and Notion into a time-aware knowledge graph. Teammates search it from Claude, Cursor and Claude Code through a local MCP server, or from the terminal. The main question it answers: "Why did we do X, who decided, and where's the evidence?"

Repo: https://github.com/PhaseYon/tacit · License: Apache 2.0 · Owner: Ahmed Fezan

## ⚠ Direction change (2026-10-26): local-first

**The source of truth for what to build is [`docs/design/local-first.md`](docs/design/local-first.md).** Read it before starting any work.

- The code currently on `main` is the **old hosted architecture**: Next.js + Better Auth, FastAPI, Celery + Redis, Postgres with RLS, OAuth MCP over HTTP, and the LangGraph Ask agent. It will be tagged `v0-hosted` and replaced step by step (design doc §13).
- **Don't extend the hosted stack.** Don't add features to `apps/web`, FastAPI routes, Celery tasks, webhooks, Postgres migrations or the Ask agent. They are being deleted.
- The older design docs (`github-connector.md`, `graph-layer.md`, `slack-connector.md`, `notion-connector.md`) describe `v0-hosted`. They're still useful for API facts and Graphiti behaviour (graph-layer §0), but not for architecture.

## Goals

- A portfolio project that shows a well-built local-first tool: Graphiti GraphRAG, a local MCP server, incremental sync, and honest benchmarks. Each piece there for a product reason.
- Fully open source. Finish a working version before adding scope.
- Publish a benchmark of Graphiti hybrid vs vector-only vs keyword-only search on about 50 "why" questions, including extraction cost and query latency.

## Core ideas

- **Local-first, we host no team data.** Each teammate runs `uvx tacit`. Sync, extraction and the MCP server run on their laptop. The team hosts one Neo4j (Aura Free by default, or Docker on a VPS).
- **Shared responsibility for access.** One password per team graph. Whoever has it can read the graph. Handing it out carefully is the team's job. **Sync scope** (which repos, channels and pages) is the permission boundary: nothing outside scope is ever fetched, Slack private channels need an explicit opt-in, and DMs are never synced.
- **Every DB access goes through `run_query()`**, which writes a `:TacitAccess` log entry first and fails closed if the write fails. The LLM never writes Cypher. MCP tools build fixed queries from their parameters.
- **Incremental, idempotent sync** by polling each source's API with the user's own token. Cursors and a single write lease live in Neo4j, and a content-hash check skips anything already ingested. Retry on rate limits.
- **Deterministic reads.** `search` uses no LLM. The AI client writes the answer itself; Tacit has no `ask` feature. LLMs are used only for Graphiti extraction during sync, with the user's own key.
- **Decisions change over time.** Use Graphiti's time-aware facts (when a fact was valid vs. when it was learned) to answer with the current decision plus its history.
- **One identity per person across tools.** Slack, GitHub and Notion accounts for the same person map to one entity.
- **Citations everywhere.** Every result links back to the exact source message, PR, issue or page.

## Stack

| Layer | Choice |
|---|---|
| Package / CLI | Python 3.13, uv, Typer (`uvx tacit`) |
| Graph + search | Graphiti on Neo4j (team-hosted); Graphiti's own hybrid search. No separate search index unless a benchmark shows it helps. |
| Embeddings | Local fastembed (bge-small, 384 dimensions) |
| MCP | FastMCP over stdio (local) |
| LLMs | Anthropic / OpenAI behind one replaceable interface, extraction only |
| Secrets | OS keychain (`keyring`) |
| Website | Next.js in `site/` on Vercel: pitch, live stats, live demo search over a fictional-data Neo4j |

## Build order

See `docs/design/local-first.md` §13 for detail.

1. Tag `v0-hosted`, restructure into the `tacit` package, delete the hosted pieces
2. `store/`: Neo4j schema, `run_query()` + access log, leases, cursors, hash skip
3. `tacit init`, `auth`, `scope`, `doctor`
4. GitHub sync end to end
5. Local MCP (`search`, `get_document`, `status`) and `mcp install`
6. Slack and Notion sync
7. People linking, `tacit log`
8. Demo load and benchmark re-run
9. Website

## Things to keep in mind

- Watch LLM extraction cost per document from day one. Graphiti makes several LLM calls for each item it ingests.
- Benchmark numbers must be real and reproducible. Don't overstate results. Nothing goes on the website until it's measured.
- The demo uses generated or our own data. Never commit real private team data.
- Make small commits after each feature or iteration, including in subagents.
