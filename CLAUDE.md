# Tacit

**Your team's memory, wired into every AI tool.**

Tacit syncs a team's Slack, GitHub and Notion into a permission-aware knowledge graph. Users search it with hybrid GraphRAG, in the web app or from Claude and Cursor over MCP. The main question it answers: "Why did we do X, who decided, and where's the evidence?"

Repo: https://github.com/PhaseYon/tacit · License: Apache 2.0 · Owner: Ahmed Fezan

## Goals

- A portfolio project that shows full-stack, hybrid GraphRAG and two-way MCP working together, with each piece there for a product reason.
- Fully open source. Finish a working version before adding scope.
- Publish a benchmark: hybrid GraphRAG vs. vector-only RAG on about 50 "why" questions, including cost and latency.

## Core ideas

- **Two-way MCP**
  - **Out:** a FastMCP server with OAuth, so Claude Desktop, Cursor and Claude Code can query the team's knowledge. Each user only sees data they're allowed to see.
  - **In:** the agent can make live lookups through other tools' MCP servers (GitHub, Linear, etc.).
  - **Bulk sync does not use MCP.** It goes through each source's API and webhooks.
- **Permission-aware retrieval.** Sync each source's permissions (Slack channel membership, repo access, Notion sharing) and filter every query by them, including MCP calls. This must never leak data.
- **Incremental, idempotent sync.** Use webhooks plus periodic backfill. Deduplicate, retry on rate limits, and never re-process unchanged content.
- **Decisions change over time.** Use Graphiti's time-aware facts (when a fact was valid vs. when it was learned) to answer with the current decision plus its history.
- **One identity per person across tools.** Slack, GitHub and Notion accounts for the same person map to one entity.
- **Citations everywhere.** Every answer links back to the exact source message, PR, issue or page.

## Stack

| Layer | Choice |
|---|---|
| Frontend | Next.js, Better Auth or Clerk |
| API | Python, FastAPI |
| Search | PostgreSQL, pgvector, Postgres full-text search |
| Graph | Graphiti, Neo4j |
| Agent | LangGraph (keep it to one agent with tools until the benchmark shows more is needed) |
| Workers | Celery, Redis |
| MCP | FastMCP |
| Tracing | Langfuse |
| LLMs | Anthropic / OpenAI behind one replaceable interface |
| Deploy | Docker Compose first, then AWS or Railway |

Graphiti has its own hybrid search. Don't keep a duplicate index unless a benchmark shows it helps.

## Build order

1. Auth and workspaces (users, teams, invites)
2. GitHub connector first (cleanest API; test on our own repos)
3. Ingestion and hybrid search with citations
4. Graph layer: people, projects, decisions, changes over time
5. MCP server with OAuth; demo Claude answering "why did we do X?"
6. Slack and Notion connectors, with permission sync
7. Demo dataset: a fictional startup with generated Slack, GitHub and Notion history, plus the benchmark

Steps 1–5 are the minimum version; 6–7 make it stand out.

## Things to keep in mind

- Watch LLM extraction cost per document from day one. Graphiti makes several LLM calls for each item it ingests.
- Benchmark numbers must be real and reproducible. Don't overstate results.
- The demo uses generated or our own data. Never commit real private team data.
