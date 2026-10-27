# Quickstart

From nothing to Claude answering "why did we do X?" about your team, in about 10 minutes.
One teammate does steps 1 and 4 once; everyone does 2, 3, 5 and 6.

## 1. Create the team graph (once per team)

Create a free instance at [Neo4j Aura](https://neo4j.com/cloud/aura-free/). Save the
connection URI (`neo4j+s://xxxx.databases.neo4j.io`) and the password it shows you once.

Aura Free holds 200k nodes / 400k relationships (plenty for a small team), pauses after
3 days without activity and is deleted after 30 idle days, so keep syncing at least every
few days (`tacit sync --watch 60` or a cron job). Prefer your own server?
See [deploy/neo4j](../deploy/neo4j/README.md).

Share the URI and password with teammates the way you share other team secrets. **Anyone
with the password can read the whole team graph**: that's the trust model (design doc §1).

## 2. Install

Needs Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/PhaseYon/tacit
```

(Once Tacit is on PyPI this becomes `uv tool install tacit`, or `uvx tacit` to run without installing.)

## 3. Connect

```bash
tacit init          # asks for the URI, password and your email (recorded in the access log)
tacit doctor        # checks everything
```

The password goes to your OS keychain (or a 0600 file where there is none, e.g. WSL).

## 4. Choose what the graph syncs (once per team)

Scope is the permission boundary: nothing outside it is ever fetched.

```bash
tacit auth github                         # reuses `gh auth token` if you have the GitHub CLI
tacit scope add github:acme/api
tacit scope add github:acme/web

tacit auth slack                          # user token from your team's Slack app: infra/slack/README.md
tacit scope add slack:#eng                # public channels; private ones need --private

tacit auth notion                         # your team's integration token: infra/notion/README.md
tacit scope add notion:https://www.notion.so/acme/Engineering-0123abcd...

tacit scope ls
```

DMs are never syncable. A private Slack channel can only be added with `--private`,
because everyone with the graph password will be able to read it.

## 5. Sync

```bash
tacit sync                  # fetch, skip unchanged, index locally (free)
tacit sync --watch 30       # keep it fresh every 30 minutes
```

Each teammate syncs with their own tokens, so between you everything in scope stays
current. If someone else is syncing at that moment, your run exits and the next one
continues from the shared cursors.

**Optional graph facts.** With an LLM key, Tacit also extracts people, decisions and
time-aware facts ("we moved from Memcached to Redis in March"):

```bash
tacit auth anthropic
tacit sync --extract --max-usd 2      # hard cap per run; spend shows in `tacit log`
```

## 6. Use it from Claude, Cursor or Claude Code

```bash
tacit mcp install --client claude          # or cursor / claude-code
```

Restart the client and ask: *"Why did we switch to Redis, and who decided?"* It calls
Tacit's `search` tool and answers with links to the exact PRs, threads and pages.

From the terminal, with no LLM involved:

```bash
tacit search "why redis"
tacit get https://github.com/acme/api/pull/41
```

## Who looked at what

Every read and write is logged in the graph:

```bash
tacit log --since 7 --grep redis
tacit log --who dana@acme.com
```
