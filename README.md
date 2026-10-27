# Tacit

**Your team's memory, wired into every AI tool.**

Tacit syncs your team's GitHub, Slack and Notion into a time-aware knowledge graph that
your team owns, and serves it to Claude, Cursor and Claude Code through a local MCP
server. Ask *"why did we do X, who decided, and where's the evidence?"* and get an answer
with links to the exact PRs, threads and pages.

- **Local-first.** Each teammate runs `tacit` on their own machine: sync, indexing and
  the MCP server all run locally. The only shared piece is your team's Neo4j (Aura Free
  or your own server). We host nothing and never see your data.
- **Free by default.** Search uses local embeddings and full-text, with no API keys.
  LLM extraction of people, decisions and time-aware facts is optional, uses your own
  key, and has a hard budget per run.
- **Deterministic reads.** `search` never calls an LLM. Your AI client writes the
  answer; Tacit returns the evidence.
- **Every access logged.** Every read and write goes through one function that logs it
  first, and refuses to run if it can't. `tacit log` answers "who looked at what".

```
 Each teammate's machine                                  Your team's Neo4j
┌──────────────────────────────────────────────┐         ┌─────────────────────────────┐
│ Claude / Cursor ──stdio──▶ tacit mcp         │         │ docs + chunks (search)      │
│                            (search, get_doc) │         │ graph facts (optional)      │
│ tacit sync ──▶ GitHub / Slack / Notion       │──Bolt──▶│ scope, cursors, write lease │
│   (your own tokens, only what's in scope)    │         │ access log                  │
└──────────────────────────────────────────────┘         └─────────────────────────────┘
```

## Quickstart

```bash
uv tool install git+https://github.com/PhaseYon/tacit
tacit init                                # your team's Neo4j URI + password, your email
tacit auth github && tacit scope add github:acme/api
tacit sync
tacit mcp install --client claude         # or cursor / claude-code
```

Full walkthrough, including Slack, Notion and graph facts: [docs/quickstart.md](docs/quickstart.md).

## Trust model

Tacit is built for small, high-trust teams:

- **One password per team graph.** Whoever has it can read everything in the graph.
  Handing it out carefully is the team's job.
- **Scope is the permission boundary.** Only the repos, channels and pages you add are
  synced. Private Slack channels need an explicit `--private`; DMs are never synced.
- **Access log.** Every query is logged with who, what and when, and kept 90 days by
  default. Known gap: someone with the password can query Neo4j directly and bypass the
  log. Teams that need more than this need a hosted gateway (design doc §11), which is
  not built.

## Commands

| Command | What it does |
|---|---|
| `tacit init` | Connect to the team graph and prepare it (safe to re-run) |
| `tacit auth github\|slack\|notion\|anthropic\|openai` | Store a token (keychain, or a 0600 file) |
| `tacit scope add\|ls\|rm` | What the graph syncs |
| `tacit sync [--extract] [--max-usd N] [--watch MIN]` | Pull new and changed items |
| `tacit search "…" [--as-of DATE] [--json]` | Hybrid search with citations, no LLM |
| `tacit get <url>` | Full stored text of a cited item |
| `tacit log [--who] [--since] [--grep]` | Who read or wrote what |
| `tacit forget <url>` | Remove one item and its graph facts |
| `tacit mcp` / `tacit mcp install` | Local MCP server / client config |
| `tacit doctor` | Check config, secrets, database and tokens |

## How it works

- **Sync** polls each source with the user's own token, skips anything whose content
  hash hasn't changed, and writes under a single write lease so two laptops never
  ingest the same thing twice. Cursors live in the graph, so whoever syncs next picks
  up where the last person stopped.
- **Search** fuses vector search (local bge-small embeddings) and full-text search over
  chunks with reciprocal rank fusion. With extraction on, it adds
  [Graphiti](https://github.com/getzep/graphiti) facts that know when they became true
  and when they were superseded.
- Details and trade-offs: [docs/design/local-first.md](docs/design/local-first.md).

## Benchmark

52 "why" questions over the fictional Larkspur Labs company ([demo/](demo/)), local
bge-small embeddings, k = 10, cost $0, extraction off. Reproduce with
`uv run python benchmark/bench.py --neo4j-uri bolt://localhost:7688 --wipe`; three runs
gave identical retrieval numbers.

| arm | recall@5 | recall@10 | MRR |
|---|---|---|---|
| vector-only | 0.846 | 0.942 | 0.711 |
| keyword-only | 0.885 | 0.962 | 0.707 |
| hybrid (RRF) | 0.885 | 0.962 | 0.704 |
| graph facts | not run (needs an LLM key) | | |

Caveats, in full in [benchmark/README.md](benchmark/README.md):
- **Hybrid is no better than keyword-only here.** The same author wrote the questions and
  the corpus, so they share wording; a held-out, paraphrased question set is still needed.
- **Not comparable one-to-one with v0** (hybrid recall@10 0.865 on Postgres): v0 indexed
  every comment as its own document, while the new stack bundles a PR or issue with its
  discussion, so a top-10 hit is easier.
- The graph-facts layer's effect on retrieval is unmeasured, so there's no claim that it
  helps. A real single-item extraction (Claude Haiku 5.5 on one decision record: 13 facts
  incl. who decided and what it superseded, 20 s, $0.007) is recorded in
  [benchmark/extraction/](benchmark/extraction/); reproduce with
  `benchmark/extraction_smoke.py`.

## Development

```bash
uv sync
docker compose --profile test up -d --wait neo4j-test   # throwaway Neo4j on :7688
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

`docker compose up -d neo4j` starts a persistent local Neo4j on :7687 for trying Tacit
solo. The previous hosted architecture (Next.js, FastAPI, Celery, Postgres RLS, OAuth
MCP) is preserved at tag [`v0-hosted`](https://github.com/PhaseYon/tacit/tree/v0-hosted).

## License

Apache 2.0
