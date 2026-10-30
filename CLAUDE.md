# Tacit

**Your team's memory, wired into every AI tool.**

Tacit syncs a team's Slack, GitHub and Notion into a time-aware knowledge graph. Teammates search it from any MCP-capable agent (Claude Code, Cursor, Claude Desktop) through a local MCP server, or from the terminal. The main question it answers: "Why did we do X, who decided, and where's the evidence?"

Repo: https://github.com/PhaseYon/tacit · Site: https://tacit-silk.vercel.app · License: Apache 2.0 · Owner: Ahmed Fezan

## Status

The local-first version is built and on `main` (design: [`docs/design/local-first.md`](docs/design/local-first.md)). The previous hosted architecture (Next.js app, FastAPI, Celery, Postgres RLS, OAuth MCP) is preserved only at tag `v0-hosted`; don't bring it back. The older design docs (`github-connector.md`, `graph-layer.md`, `slack-connector.md`, `notion-connector.md`) describe `v0-hosted`; they're still useful for API facts and Graphiti behaviour (graph-layer §0), not for architecture.

## Goals

- A portfolio project that shows a well-built local-first tool: GraphRAG, a local MCP server, incremental sync, and honest benchmarks. Each piece there for a product reason.
- Fully open source. Finish what exists before adding scope.
- Keep the benchmark ([`benchmark/`](benchmark/)) real and reproducible: hybrid vs vector-only vs keyword-only vs graph facts on 52 "why" questions, with cost and latency.

## Core ideas

- **Local-first, we host no team data.** Each teammate installs `tacit`. Sync, indexing, extraction and the MCP server run on their machine. The team hosts one Neo4j (Aura Free, or Docker on a VPS via `deploy/neo4j`).
- **Shared responsibility for access.** One password per team graph; whoever has it can read the graph. **Sync scope** (repos, channels, pages) is the permission boundary: nothing outside scope is fetched, private Slack channels need an explicit opt-in, DMs are never synced.
- **Every DB access goes through `Store.run_query()`** (or `Store.access()` for Graphiti), which writes a `:TacitAccess` log entry first and fails closed if it can't. The LLM never writes Cypher; MCP tools build fixed queries from their parameters.
- **Incremental, idempotent sync** by polling each source with the user's own token. Cursors and a single write lease live in Neo4j; a content-hash check skips anything already ingested.
- **Two search layers.** Chunks (local bge-small embeddings + full-text, fused with RRF) are always on and free. Graph facts (Graphiti) are optional and need an LLM key for extraction. `search` is deterministic and calls no LLM; the AI client writes the answer. There is no `ask` feature.
- **Decisions change over time.** Graphiti facts carry when they were valid, so answers give the current decision plus its history.
- **One identity per person across tools** (`tacit people`).
- **Citations everywhere.** Every result links to the exact PR, issue, message or page.

## Layout

| Path | What |
|---|---|
| `src/tacit/` | The package: `cli.py`, `sync.py`, `search.py`, `mcp.py`, `store/` (Neo4j, access log, lease, cursors), `sources/` (GitHub, Slack, Notion), `graph/` (Graphiti, LLM adapter, budget), `llm/` (Anthropic, OpenAI, record/replay) |
| `tests/` | pytest against a throwaway Neo4j: `docker compose --profile test up -d --wait neo4j-test` (port 7688, wiped by tests) |
| `benchmark/` | `bench.py`, `extract_demo.py`, `extraction_smoke.py`, results |
| `demo/` | The fictional Larkspur Labs dataset; `tacit demo load` |
| `site/` | Next.js website, deployed on Vercel (root directory `site`) |
| `deploy/neo4j/` | Self-hosted Neo4j for teams |

## Stack

| Layer | Choice |
|---|---|
| Package / CLI | Python 3.13 (pinned in `.python-version`), uv, Typer |
| Graph + search | Neo4j: chunk index (vector + full-text) and Graphiti facts |
| Embeddings | Local fastembed (bge-small, 384 dimensions) |
| MCP | FastMCP over stdio |
| LLMs | Anthropic / OpenAI behind one interface, extraction only |
| Secrets | Env vars → OS keychain → 0600 file |
| Website | Next.js on Vercel |

## Working on it

- Run `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` before committing, and check that GitHub CI is green before calling a PR done.
- Watch LLM extraction cost. Graphiti makes several LLM calls per item; extraction has per-run budget caps.
- Benchmark numbers must be real and reproducible. Don't overstate results, don't tune on the 52 benchmark questions, and nothing goes on the website until it's measured.
- The demo uses generated data only. Never commit real private team data.
- Make small commits after each feature or iteration, including in subagents.
