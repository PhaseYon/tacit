# Design: Graph layer (build step 4)

Status: draft for review · Owner: Ahmed Fezan · Date: 2026-10-24

Step 4 turns the "why" documents from step 2 into a time-aware knowledge graph with
Graphiti: people, components, technologies and decisions, plus facts that know when they
became true and when they stopped being true. Users must only ever see facts from
sources they can read. Every fact must link back to a source URL. Spend must be capped
per workspace.

Non-goals: Slack/Notion (step 6), the agent and MCP tools (step 5; §8 fixes the
contract), communities, and LLM work on documents the step-2 gate excludes.

---

## 0. Facts we rely on (graphiti-core 0.31.2)

Sources: [PyPI graphiti-core 0.31.2](https://pypi.org/project/graphiti-core/0.31.2/)
(released 2026-10-20), its wheel source (read directly, file references below), and
[getzep/graphiti](https://github.com/getzep/graphiti). Third-party estimate of LLM calls:
[mintlify.com/getzep/graphiti/advanced/performance-tuning](https://mintlify.com/getzep/graphiti/advanced/performance-tuning).

| Topic | What 0.31.2 actually does |
|---|---|
| Backends | Neo4j ≥ 5.26 (core dependency); FalkorDB ≥ 1.1.2 (`[falkordb]`, `[falkordblite]`); Neptune plus OpenSearch Serverless (`[neptune]`); Kuzu (`[kuzu]`) is **deprecated, removal planned**. |
| `group_id` | On Neo4j it is a **property** on every node and edge in one database, and searches add `group_id IN $group_ids`. On FalkorDB each group is a **separate graph**; multi-group searches fan out one query per graph and merge (`decorators.handle_multiple_group_ids`). Allowed characters: `[A-Za-z0-9_-]`. |
| ⚠ Empty group list | `search/search.py`: `group_ids = group_ids if group_ids and group_ids != [''] else None`. **`[]` means "search every group".** `clear_data(driver, group_ids=None)` deletes everything. |
| ⚠ Full-text limit | Neo4j BM25 returns nothing once query words + number of groups ≥ 128 (`_build_neo4j_fulltext_query`, `MAX_QUERY_LENGTH = 128`). |
| Extraction scope | Context and dedup are limited to the episode's own group: previous episodes (`retrieve_episodes(group_ids=[group_id])`), node candidates (`node.group_id`) and edge resolution (`group_ids=[edge.group_id]`). Nothing crosses groups. |
| `add_episode` | Takes name, body, source_description, `reference_time`, `source` (message/text/json), `group_id`, `entity_types`, `edge_types`, `edge_type_map`, `excluded_entity_types` and `custom_extraction_instructions`. Episodes in one group must be added **one at a time, in order**. `uuid=` only loads an existing episode, so we can't pick uuids for new ones. |
| `add_episode_bulk` | One combined node+edge extraction call per episode (`use_combined_extraction=True`), plus `strict_edge_types`. Edge invalidation also runs in bulk. |
| LLM calls / episode | Node extraction, LLM dedup only when the deterministic and similarity steps can't decide, edge extraction, edge timestamp extraction and dedup, attribute and summary extraction. Roughly **6–10 calls**; the third-party estimate is 6–10. |
| `remove_episode` | Deletes edges and nodes that only this episode supported, then **rebuilds the summaries of surviving nodes with the LLM** (costs money). It does **not** undo invalidations this episode caused on older edges. It uses the default driver. |
| Bi-temporal edges | `EntityEdge` has `valid_at`, `invalid_at` (world time), `created_at`, `expired_at` (system time), `reference_time`, and `episodes: list[uuid]` (provenance). `SearchFilters` filters any of these with `is_null` / `is_not_null` / comparisons, plus `node_labels`, `edge_types` and `property_filters`. |
| Search | `search()` returns edges with RRF; `search_(query, config, group_ids, search_filter, center_node_uuid, …)` uses recipes (`EDGE_HYBRID_SEARCH_RRF`, `COMBINED_HYBRID_SEARCH_RRF`, `…_CROSS_ENCODER`, `…_NODE_DISTANCE`). |
| LLM clients | OpenAI (default), Anthropic, Gemini, Groq, Azure, OpenAI-compatible generic. Stock `AnthropicClient` default is `claude-haiku-4-5-latest`; it **forces `tool_choice={"type":"tool"}`** and always sends `temperature`. Token usage is recorded per `prompt_name` in `llm_client.token_tracker`. Optional disk cache: `LLMClient(cache=True)`. |
| Embedder | `EmbedderClient` with `create` / `create_batch`. The dimension comes from the `EMBEDDING_DIM` env var, read at import time (default 1024). Built in: OpenAI, Azure, Gemini, Voyage. No sentence-transformers embedder ships (that extra only powers `BGERerankerClient`). |
| Cross-encoder | Defaults to `OpenAIRerankerClient` when none is passed. `BGERerankerClient` loads `BAAI/bge-reranker-v2-m3` locally. |
| `gliner2` extra | `GLiNER2Client` runs entity recognition locally (205–340M params, CPU) and passes everything else (edges, dedup, summaries) to a wrapped LLM client. Experimental, and model routing is fixed when it's created. |
| Telemetry, concurrency | `posthog` telemetry, off via `GRAPHITI_TELEMETRY_ENABLED=false`. `max_coroutines` / `SEMAPHORE_LIMIT` (default 10) limits each gather, not the total. |

## 1. Graph backend

**Decision: Neo4j 5.26 LTS (Community), already in compose under profile `graph`.** Pin
the image to `neo4j:5.26` and cap the heap at 1 GB. Neo4j is Graphiti's primary,
best-tested backend, and on Neo4j `group_id` is a filterable property, so one query
covers all of a user's readable containers (§3).

| Rejected | Why |
|---|---|
| FalkorDB | One graph per group is tidy isolation, but a user with 40 readable repos means 40 queries per search, merged in Python. `remove_episode` runs on the default driver, which is the wrong graph for any named group. SSPL license sits badly with an Apache-2.0 demo. Revisit only if Neo4j memory becomes a real problem. |
| Kuzu | Deprecated in 0.31; removal planned. |
| Neptune | AWS-only, needs OpenSearch Serverless too, and costs money even when idle. |

Neo4j Community has no RBAC, so any process with the credentials can read every group.
We handle that in code structure instead (§3.4).

## 2. LLM, embedder and reranker behind Tacit's provider interface

**Tacit interface (`tacit_api/llm/`):** one protocol,
`complete_structured(messages, schema: type[BaseModel], tier: "small"|"default", max_tokens) -> (dict, Usage)`,
with `AnthropicProvider`, `OpenAIProvider` and `ReplayProvider` (tests, §9). Selected by
`LLM_PROVIDER` (`anthropic` | `openai` | `replay`), with `LLM_MODEL` and `LLM_SMALL_MODEL`.
The step-5 agent uses the same interface.

**Graphiti adapter: `TacitGraphitiLLMClient(graphiti_core.llm_client.LLMClient)`.** It
implements `_generate_response` by calling the provider, and overrides
`generate_response` to record usage per `prompt_name` (§7.4) and enforce the budget
before each call. We don't use Graphiti's stock `AnthropicClient` because:
- It forces `tool_choice: tool`, which current-generation Claude models (Opus 5.5,
  Sonnet 5.5) reject with a 400. Our provider uses structured outputs
  (`output_config.format`) for Anthropic and `json_schema` for OpenAI, so model upgrades
  stay a config change.
- We need per-workspace and per-episode cost attribution and a hard stop inside the
  episode, not only afterwards.
- It gives us one place to swap in the replay provider.

**Default models:** Anthropic `claude-haiku-4-5` ($1 / $5 per MTok) for both tiers at
first, because it's the model Graphiti's prompts are tuned and tested on. Evaluate
`claude-haiku-5-5` ($0.10 / $0.50 per MTok, 10× cheaper) during dogfood (plan step 9) and
switch if extraction quality holds. OpenAI: whatever `OpenAIProvider` is configured with,
at the same tiers. Prices go in a versioned `llm/pricing.py` table, not in the database.

**Embedder: yes, reuse local fastembed `bge-small-en-v1.5` (384-dim)** through a
`TacitEmbedder(EmbedderClient)` that wraps `search.embeddings.get_embedder()`
(`create_batch` → `embed_documents`, `create` → `embed_query`) and sets
`EMBEDDING_DIM=384` before Graphiti is imported. It's free, already loaded in the API and
workers, has no key, and is reproducible for the benchmark. The same
`EMBEDDING_PROVIDER=openai` switch from step 3 applies.
*Is it wise?* Mostly. Graphiti embeds short strings (entity names, one-sentence facts),
and bge-small is weaker on those than `text-embedding-3-*`. Weaker similarity costs us in
two places: more dedup candidates fall through to the LLM (cost), and more facts are
missed in search (quality). Both are measurable. The benchmark compares bge-small with
OpenAI-384, and a dogfood metric tracks the "LLM dedup calls per episode" ratio.
Changing the embedder means rebuilding the graph (§7.4), so record `embedding_model` on
`graph_episode`.

**Cross-encoder: none.** Pass a `NoRerank` client that raises if called (this stops
Graphiti from building the OpenAI default), and only use `*_RRF` / `*_NODE_DISTANCE`
recipes. The benchmark may later add `BGERerankerClient` (a local 568M model) as one arm.

**gliner2: not in v1.** It saves only the node-extraction call, the other ~5–9 calls
remain, and it's experimental. We can try it as a cost arm once usage data shows which
`prompt_name` dominates.

**Cost estimate (to replace with measured numbers):** about 8 calls × ~3.5k input
+ ~0.4k output tokens comes to ~28k input / 3k output per episode. That's ≈ **$0.045 per
episode on Haiku 4.5** and ≈ **$0.004 on Haiku 5.5**. A repo with 1,500 eligible
episodes would cost about $68 (Haiku 4.5) or $6 (Haiku 5.5) to backfill.
`remove_episode` adds summary rebuild calls on edits.

## 3. Permission model (never leak)

### 3.1 Decision: one Graphiti group per `source_container`

`group_id = "c-" + source_container.id` (a uuid, which matches `[A-Za-z0-9_-]`). Every
episode, entity and fact belongs to exactly one container. A query searches only the
groups for containers the user can read *right now*, and the existing Postgres function
`app.can_read_container` decides that.

Why this is safe by construction: Graphiti never mixes groups during extraction (§0). So
an entity's **summary**, a fact's **text** and an **invalidation** are all derived only
from documents in that container. Nothing private can be inside a public group's output.

### 3.2 Rejected alternatives

| Option | Why not |
|---|---|
| One group per workspace + post-filter facts by provenance (edge → episode → document → container) | Graphiti merges entities and facts across all documents in a group. A Person's LLM-written summary would blend private-repo and public-repo facts, and post-filtering can't redact a summary. A private PR would also **invalidate** a public fact, and the expired fact plus its `invalid_at` would reveal that a private decision exists. Leaks by design. |
| Group per user | Re-extracts the same document once per reader: N× the cost, and ACL changes force re-extraction. |
| Group per "ACL class" (containers with identical readers) | Any ACL change moves containers between classes, so the graph would need regrouping. Hard to reason about. |

**Cost we accept:** an entity that shows up in five repos is extracted five times, as
five nodes. Cross-container identity comes from Postgres (§6), not from merging nodes.

### 3.3 How a query computes allowed groups (fail closed)

All graph reads go through `tacit_api/graph/access.py::search_facts(ctx, query, …)`,
which runs inside `rls_session(user_id, workspace_id)` as `tacit_api`:

1. `SELECT id FROM app.readable_container_ids()`. This is a new `STABLE SECURITY DEFINER`
   function, `SELECT c.id FROM source_container c WHERE app.can_read_container(c.id)`, so
   the rule lives in one place: active container, current workspace, public **or** an
   ACL row for this user that is under 6 h old.
2. **If the set is empty, return `[]` without calling Graphiti.** Passing `[]` would
   search *every* group (§0). There's an assertion and a unit test for this.
3. If the set has more than 100 groups, split it into batches of ≤ 100 (the BM25 limit,
   §0), search each batch, and merge with RRF.
4. Call `graphiti.search_(…, group_ids=allowed, search_filter=…)`.
5. **Post-check 1:** drop any edge or node whose `group_id` isn't in `allowed`, and log it
   at ERROR level. It should never happen.
6. **Post-check 2 (the real authority):** hydrate citations. Map `edge.episodes` to
   `graph_episode` rows **in the same RLS transaction**. `graph_episode` has the
   workspace policy *and* the RESTRICTIVE `app.can_read_container(container_id)` policy,
   exactly like `source_document`. Then drop any fact with no visible, `status='done'`
   episode. A fact reaches the user only if Postgres agrees they can read at least one
   source it came from, and that source gives the citation URL.

So a bug in Graphiti, a mistake in the group list, or an orphaned episode in Neo4j still
can't surface a fact. Step 6 would have to break Postgres RLS to leak, and every RLS leak
test already guards that. Stale ACLs, unlinked GitHub accounts, deactivated containers
and removed workspace members all fail closed through the same function, with no
graph-specific code. MCP calls take this same path, because MCP runs as `tacit_api` with
the user's context.

### 3.4 Containing the unscoped Neo4j credential

- Only `tacit_api/graph/` imports `graphiti_core`. A CI test greps for imports
  elsewhere. The `Graphiti` object is never returned from that package.
- The only public read functions take a `UserContext` and do steps 1–6. Writes run in
  workers and take a `container_id` from a `graph_episode` row read as `tacit_worker`
  (scoped to the workspace).
- Wrapper helpers for deletion refuse `None` or empty group lists (`clear_data(None)`
  wipes everything).
- In prod Neo4j stays inside the compose network; the password comes from env.

### 3.5 Removing and hiding data

| Event | Hidden immediately by | Purged by |
|---|---|---|
| Repo removed, archived, or uninstall | `is_active=false` → not in `readable_container_ids`, and RLS hides its `graph_episode` rows | After 30 days (same as documents): `clear_data(driver, ["c-<id>"])`, delete `graph_*` rows. Right away on uninstall if step-2 open question 4 says so. |
| ACL loses a user | `container_access` row deleted → group excluded | Nothing to purge |
| Root doc (PR, issue, markdown) tombstoned | `graph_episode.status='stale'` in the tombstone transaction → its facts fail post-check 2 | Worker: `remove_episode`, delete rows |
| Comment inside a bundle tombstoned or edited | Episode set to `stale` (its facts are hidden until rebuilt) | Worker: rebuild the bundle (§7.3) |
| Private → public or public → private | `visibility` change re-evaluated by `can_read_container` | Nothing |

## 4. Episode mapping

Eligibility follows the gate in the step-2 design. One **bundle** becomes one episode
(or a few parts):

| Bundle root | Eligible when | Members (chronological) | `reference_time` |
|---|---|---|---|
| `pull_request` | `metadata.merged_at` is set | PR body, `pr_review`s with a body, `pr_review_comment`s, `issue_comment`s on the PR; human authors only | `merged_at` |
| `issue` | `state='closed'` and ≥ 2 human `issue_comment`s (`completed` or `not_planned`) | issue body + comments | `closed_at`, or else `source_updated_at` |
| `markdown_file` | matches the repo's doc globs | the file | blob commit time (`source_updated_at`) |

- **Rendering** (`graph/render.py`, versioned with `RENDERER_VERSION`):
  `EpisodeType.message`, one line block per member,
  `@alice (PR author, 2026-03-01): <body>` / `@bob (review, approved): …` /
  `@carol (comment on api/db.py:42): …`. A header carries the repo, the PR/issue number,
  title, labels, state and approvers from metadata. Speaker labels are always
  `@<github login>`, which is what makes §6 deterministic. Diff hunks are dropped.
- **Size cap:** 12k tokens per episode (counted with the existing `count_tokens`). A PR
  over the cap keeps the body, the first 5 and last 15 comments, and a
  "[N comments omitted]" marker. Markdown over the cap is split on `##` headings into
  parts (`part = 0..n`), each with its own `graph_episode` row.
- **`name`:** `"{owner/repo}#{number}: {title}"` or `"{owner/repo}:{path}"`.
  **`source_description`:** `"github {kind} {url} tacit:{graph_episode.id}"`. The URL
  makes raw Graphiti output readable; the `tacit:` tag lets cleanup find our episodes
  (§7.3). Citations always come from Postgres, never from this string.
- **Membership:** `graph_episode_document(graph_episode_id, document_id)`. It's used for
  stale propagation and to find comment-level evidence (§8).
- **Not episodes:** open or unmerged PRs, open issues, issues with < 2 human comments,
  bot content (already filtered in step 2). Closed-unmerged PRs ("rejected
  alternatives") are open question 2.

## 5. Ontology: entity and edge types, decisions over time

Kept small, as Pydantic models in `graph/ontology.py` (`ONTOLOGY_VERSION` is part of the
input hash):

| Entity | Attributes | Meaning |
|---|---|---|
| `Person` | `github_login: str \| None` | A human participant or mentioned person |
| `Component` | `kind: repo\|service\|module\|package\|api\|other` | A part of *our* system |
| `Technology` | `category: language\|library\|database\|vendor\|tool\|other` | Something external we adopt or drop |
| `Decision` | `status: proposed\|accepted\|rejected\|superseded`, `summary` | A choice with a rationale |

| Edge | Signature | |
|---|---|---|
| `DECIDED` | Person → Decision | proposed, approved or made the call |
| `AFFECTS` | Decision → Component \| Technology | |
| `SUPERSEDES` | Decision → Decision | explicit, e.g. "replaces ADR-3" |
| `USES` | Component → Technology | |

Everything else falls back to Graphiti's default `Entity` / free-form edges (not
strict). **Authorship is not extracted.** "Who wrote PR #12" comes from
`source_document.author_*` and `metadata.approved_by` in Postgres for free.
`custom_extraction_instructions` say: name people exactly by their `@login`, extract
decisions only when there's a rationale or an explicit choice, ignore code-level detail,
and treat approvals as `DECIDED`.

**Change over time:** the reference time of each episode becomes `valid_at` on its
facts. When a later episode produces a contradicting fact *between the same entities*
(e.g. `tacit-api USES Elasticsearch` → `tacit-api USES Postgres FTS`), Graphiti sets
`invalid_at` on the old fact (world time) and `expired_at` (when we learned it).
`SUPERSEDES` records explicit replacements between Decision nodes.
- **Current decision:** `SearchFilters(invalid_at=[[is_null]], expired_at=[[is_null]])`.
- **History:** no filter, sorted by `valid_at`, with superseded facts labelled.
- **Ordering matters:** backfill adds episodes per group in `reference_time` order
  (§7.2). An out-of-order late episode still gets correct `valid_at`, and Graphiti
  compares timestamps when deciding which fact invalidates which.
- **Known weak spot:** invalidation only fires if both mentions resolve to the same
  nodes. That depends on entity resolution and the embedder (§2), and the benchmark
  measures it.

## 6. Identity: Person nodes ↔ Tacit people

- **New table `person`** (workspace-scoped). In step 4 every `external_identity` (GitHub
  user) gets exactly one `person` row, created at upsert, and
  `external_identity.person_id` is set. Step 6 merges Slack and Notion identities into
  the same person **only through verified links** (same rule as ACLs: OAuth-proven, never
  email or name).
- **Linking nodes deterministically:** after `add_episode` returns, for each result node
  labelled `Person`, normalize `node.name` (strip `@`, lowercase) and match it against
  the bundle's **participant map** `{login → external_identity}`. The renderer builds
  that map from member authors and approvers, plus `@mentions` resolved through
  `external_identity.login`. On an exact match:
  - set `attributes.github_login` on the node
  - write `graph_entity_link(group_id, node_uuid, external_identity_id, person_id,
    method='login_exact')`

  No match leaves the node unlinked. We never fuzzy-match names with an LLM.
- **Effect on extraction:** because speakers are always `@login`, the next episode in the
  same group extracts the same name, and Graphiti's exact-name candidate step resolves it
  without an LLM dedup call.
- **"One identity" across containers:** the same person is one node per group, and all
  those nodes link to one `person_id`. Asking "what did Alice decide" resolves
  `person_id` → that person's node uuids **within the allowed groups** →
  `SearchFilters(edge_source_node_uuids=…)` or `center_node_uuid`. The graph never
  merges nodes across groups, so a private repo's node is never used for a user who
  can't read it.
- `graph_entity_link` has the same RESTRICTIVE container policy as `graph_episode`.

## 7. Pipeline, idempotency and cost control

### 7.1 Schema (migration 0005)

```text
graph_episode            RLS workspace + RESTRICTIVE can_read_container (tacit_api SELECT)
  id uuid pk, workspace_id, container_id, root_document_id → source_document, part int
  group_id text, episode_uuid text null, input_hash bytea, embedding_model text
  status text   -- pending|processing|done|stale|failed|skipped_budget
  token_estimate int, attempts int, error text, processed_at timestamptz
  UNIQUE (root_document_id, part)
graph_episode_document   (graph_episode_id, document_id) pk, workspace_id, container_id   same RLS
graph_entity_link        workspace_id, container_id, group_id, node_uuid, entity_type,
                         external_identity_id, person_id, method                     same RLS
person                   id, workspace_id, display_name, avatar_url                RLS workspace
llm_usage                id, workspace_id, container_id null, graph_episode_id null,
                         purpose (graph_extract|graph_rebuild|agent), provider, model,
                         prompt_name, input_tokens, cached_input_tokens, output_tokens,
                         cost_usd numeric(12,6), created_at                  RLS workspace (admins read)
workspace_graph_settings workspace_id pk, enabled bool default false,
                         monthly_budget_usd numeric default 5, paused_reason text
app.readable_container_ids()   → tacit_api   (§3.3)
app.workspaces_pending_graph() → tacit_worker
```

`input_hash = sha256(RENDERER_VERSION, ONTOLOGY_VERSION, model ids, embedding_model,
sorted (document_id, content_hash) of all members)`. It is computed in SQL from a
`graph_bundle_candidates` view, which applies the §4 eligibility rules. A bundle needs
work when `input_hash IS DISTINCT FROM graph_episode.input_hash`. This is the graph's own
"processed hash". It doesn't reuse `source_document.processed_hash`, which the step-3
indexer owns. Metadata-only changes (reactions, approvals) don't change `content_hash`,
so they don't cost tokens. Approvals do appear in the rendered header, but a new
approval alone is deliberately not re-extracted.

### 7.2 Flow

```text
beat (5 min) → graph_scan(ws): diff view vs graph_episode → upsert rows status=pending
            → enqueue graph_process(group) per container with pending rows
graph_process(group):  Redis lock "graph:{group}" (Graphiti needs sequential episodes per group)
   for row in pending ORDER BY reference_time LIMIT 20:
       budget check → render → cleanup old → add_episode → link people → commit PG
```

- Queue `graph`, concurrency 2 (env). Parallel across containers, serial within one.
  Graphiti `max_coroutines=4`.
- **Rate limits:** the SDK retries 429/5xx (`max_retries=3`). A persistent 429 re-queues
  the task with a countdown instead of holding the lock.
- **Feature flag:** `GRAPH_ENABLED` defaults to false. It's forced off when the selected
  provider has no API key or Neo4j isn't configured. A workspace also needs
  `workspace_graph_settings.enabled = true`. The UI shows the estimated backfill cost from
  `token_estimate` sums (the `connector_stats` view from step 2) before an admin turns it
  on. Telemetry is off (`GRAPHITI_TELEMETRY_ENABLED=false`).

### 7.3 Idempotency and edits

Per row:
1. PG: `status='processing'`, `attempts += 1`.
2. **Cleanup:** remove the episode in `episode_uuid` if set, *and* any episode in the
   group whose `source_description` contains `tacit:{row.id}`. That covers a crash
   between the Graphiti write and the PG commit.
3. `add_episode(…)`, then link people (§6).
4. PG: set `episode_uuid`, `input_hash`, `status='done'`, write `llm_usage`, and replace
   `graph_episode_document`.

Re-runs converge after any crash. Orphans are invisible meanwhile (post-check 2 needs a
`done` row), and an hourly sweep removes Neo4j episodes with no `graph_episode`.

**Edits** use remove-then-re-add. Caveat (§0): `remove_episode` doesn't restore facts
that the removed episode had invalidated. That's rare for merged PRs and ADRs. The fix
is `graph_rebuild_container(container_id)`, an admin action and nightly opt-in, which
runs `clear_data(["c-<id>"])` and replays all `done` rows in `reference_time` order. The
same task handles ontology, renderer or embedder version bumps. It costs money, so it
runs under the budget check like everything else.

### 7.4 Cost tracking and the hard stop

- `TacitGraphitiLLMClient` reads a `contextvar` (workspace, container, graph_episode)
  that the task sets. `asyncio.gather` copies it into Graphiti's sub-tasks. Each call
  appends a usage record, flushed to `llm_usage` in step 4 of §7.3, with
  `cost_usd = tokens × pricing[model]`.
- **Before each episode:** if `month_spend + estimate > monthly_budget_usd`, set
  `status='skipped_budget'`, set `workspace_graph_settings.paused_reason='budget'`, stop
  that workspace's scan, and show a UI banner. The estimate is
  `token_estimate × 10 + 3k` (calibrated from `llm_usage` after dogfood).
- **During an episode:** before every LLM call, the client checks
  `month_spend + episode_running_cost ≤ budget` (read once per episode, then counted in
  memory) and raises `BudgetExceeded` otherwise. Graphiti only persists in
  `_process_episode_data` after extraction, so an abort mid-extraction writes nothing.
  Plan step 5 verifies this with a test.
- Global safety net: `GRAPH_MAX_USD_PER_DAY` (env), same mechanism across workspaces.
- Reports: `graph_cost_stats` view (per workspace and repo: episodes, $ total,
  $/episode, calls per episode, tokens per `prompt_name`). These numbers go into the
  benchmark write-up.

**Embedder changes:** `embedding_model` is in `input_hash`, so switching marks every
bundle dirty; vectors from different models must never mix. Rebuild per repo.

## 8. Retrieval for the step-5 agent and MCP

Contract (`tacit_api/graph/access.py` + `search/hybrid.py`, both under the user's RLS
session):

- `search_facts(ctx, query, *, include_history=False, about_person=None, limit=20)`
  returns `Fact{fact, kind (edge name), subject, object, valid_at, invalid_at, status
  (current|superseded), citations: [Citation{url, title, kind, container, author,
  created_at}]}`. It uses `EDGE_HYBRID_SEARCH_RRF`, or `EDGE_HYBRID_SEARCH_NODE_DISTANCE`
  when centred on a person or decision node, with the current-only filter unless
  `include_history` is set.
- `search_documents(ctx, query)`: the step-3 hybrid chunk search, unchanged.
- **Fact → exact evidence:** a fact cites its bundle's root URL (PR, issue, file). To cite
  the *exact comment*, run `hybrid_search` restricted to the episode's
  `graph_episode_document` members, with the fact text as the query, and attach the best
  chunk's URL and snippet. This uses local embeddings, needs no LLM, and runs under RLS.
- **Agent pattern for "why did we do X, who decided, where's the evidence":** call
  `search_facts` (current plus history) and `search_documents` in parallel, then answer
  with: the current decision (facts with `status=current`), what it replaced
  (superseded facts with dates), who decided (`DECIDED` facts plus `approved_by` from
  Postgres), and evidence (quoted chunks). Every claim carries a URL. A fact without a
  citation can't exist (post-check 2).
- **Coverage gap:** open PRs, short issues and everything below the gate exist only in
  the chunk index, one reason both indexes stay for now.
- **Benchmark (step 7) decides what survives.** Arms: (a) vector-only chunks,
  (b) hybrid chunks, (c) graph facts only, (d) graph + chunks, each with tokens, $ and
  p50/p95 latency. If (c) ≈ (d), drop the chunk index for the graph-covered kinds. If
  (b) ≈ (d), the graph needs to earn its cost on "who/when/what changed" questions or go.
  We report numbers as measured.

## 9. Testing without spending money

- **ReplayProvider:** key = `sha256(prompt_name, model, rendered messages, schema name)` →
  JSON response in `apps/api/tests/fixtures/llm/<key>.json`. Modes are `replay`
  (default in CI; a missing key fails the test with the key printed) and `record` (live,
  opt-in, writes fixtures). Fixtures come only from synthetic `tacit-fixtures` content,
  never real private data.
- **Unit (no Neo4j):** rendering and caps, eligibility view, `input_hash` stability,
  person linking from a fake `AddEpisodeResults`, budget logic, the
  empty-allowed-set → no Graphiti call, and the >100-group batching.
- **Integration (CI service `neo4j:5.26` + Postgres, replay LLM, `FakeEmbedder` at
  384-dim):** ingest 6 recorded bundles across a public and a private container, then:
  - **leak tests:** user without access, stale ACL, deactivated container, user removed
    from the workspace, a fact from a foreign group injected straight into Neo4j
    (post-check 1), and a Neo4j episode with no PG row (post-check 2). None may return a
    private fact or entity name.
  - supersession: ADR-1 then ADR-2 leaves the old fact with `invalid_at` set, and the
    current filter hides it.
  - edit: re-render → cleanup → re-add, so no duplicate episodes. Crash injection between
    steps 3 and 4 converges.
  - purge: `clear_data` on one group leaves the other group untouched.
- **Live (opt-in, `GRAPH_LIVE_IT=1` + key):** 5 bundles from `tacit-fixtures` with a hard
  budget of $0.25. Asserts structure only (some `Decision` nodes, people linked, every
  returned fact cited), records `llm_usage`, and prints $ per episode. Also re-records
  replay fixtures when run with `LLM_REPLAY=record`.

## 10. Implementation plan

Each step is one PR-sized commit with tests.

1. **Compose and deps:** pin `neo4j:5.26` with a heap limit, add
   `graphiti-core[anthropic]==0.31.2`, settings (`GRAPH_ENABLED`, `NEO4J_*`,
   `LLM_PROVIDER`, models, `GRAPH_MAX_USD_PER_DAY`), telemetry off, the CI Neo4j
   service, and the import-boundary test.
2. **LLM provider interface:** `tacit_api/llm/` (Anthropic structured outputs, OpenAI
   json_schema, replay), pricing table, usage records.
3. **Migration 0005:** tables in §7.1, RLS + RESTRICTIVE policies, `readable_container_ids`,
   `workspaces_pending_graph`, `person` backfill from `external_identity`. RLS leak tests
   for the new tables.
4. **Graphiti wiring:** `TacitGraphitiLLMClient`, `TacitEmbedder` (384), `NoRerank`, the
   ontology, a single factory in `graph/client.py`, `build_indices_and_constraints` at
   worker start.
5. **Bundles:** `graph_bundle_candidates` view, renderer + caps, `input_hash`, participant
   map. Fixture tests, plus the "abort mid-episode writes nothing" test.
6. **Worker pipeline:** scan/process tasks, per-group lock, cleanup-then-add, person
   linking, budget checks, `llm_usage`, tombstone → `stale` hook in `ingest.tombstone`.
7. **Read path:** `search_facts` with the fail-closed steps 1–6, history filter, citation
   hydration, fact → evidence chunk, `GET /search/facts` for the web app. Graph leak test
   suite.
8. **Lifecycle:** container purge, orphan sweep, `graph_rebuild_container`, workspace
   settings + budget banner API.
9. **Dogfood:** enable on `PhaseYon/tacit` and `tacit-fixtures`. Record $/episode, calls
   per episode and dedup ratio for Haiku 4.5 vs 5.5 and bge-small vs OpenAI-384, and
   update §2 with real numbers.

## 11. Open questions

1. **Default model:** switch to `claude-haiku-5-5` if dogfood quality holds (10× cheaper)?
   Who judges "holds": a 20-question spot check, or wait for the benchmark?
2. **Closed-unmerged PRs** as "rejected alternative" episodes? They're useful for "why
   didn't we do Y", but they add maybe 20–40 % more episodes.
3. **Default monthly budget:** is $5 per workspace right for the hosted demo? Should the
   budget also cover the step-5 agent's `llm_usage`, or only extraction?
4. **Data sent to the LLM provider:** private repo text leaves our infrastructure during
   extraction. Do we need a per-workspace "allow private repos in extraction" switch
   (public-only by default), and should we document provider retention/ZDR?
5. **Nightly rebuild** to fix invalidation drift after edits (§7.3): on by default for
   small repos (< 200 episodes), or manual only?
6. **Approval-only changes** don't re-extract (§7.1). Is it acceptable for "who
   approved" to come only from Postgres metadata and not from the graph?
7. **Neo4j Community vs Enterprise/Aura** (RBAC, backups): decide at the deploy step.

## 12. Decisions on open questions (2026-10-25)

1. **Default extraction model: `claude-haiku-5-5`** (current Haiku, ~10× cheaper than
   Haiku 4.5 per the estimate above), behind Tacit's own LLM adapter. Quality is checked in
   dogfood and the benchmark; switching model bumps `input_hash` and re-extracts.
2. **Closed-but-unmerged PRs:** not in v1. Revisit after the benchmark measures whether
   "rejected alternatives" improve answers enough to justify 20–40% more cost.
3. **Budget:** $5/month per workspace default, covering graph extraction and the agent's
   LLM calls (one `llm_usage` ledger). Hard stop at the cap.
4. **Private content and LLM providers:** graph extraction is off by default. Enabling it is
   per workspace, and **private repos need a separate explicit opt-in** by a workspace admin,
   with a note that their text is sent to the configured LLM provider. Public repos only by
   default.
5. **Nightly rebuild:** manual only in v1 (a "rebuild repo" admin action and task).
6. **Approvals:** "who approved" comes from Postgres metadata, not re-extraction. Accepted.
7. **Neo4j:** Community 5.26 for self-hosting and the demo; Aura/Enterprise is a deployment
   choice, not a code change.
