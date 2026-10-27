# Notion setup

Tacit reads Notion with one **internal integration** that belongs to your team. The
integration only sees pages someone has shared with it, and Tacit only syncs the page
trees you add to the scope. Design: [docs/design/local-first.md](../../docs/design/local-first.md) §6.2–6.3.

## Once per team (a Notion workspace owner)

1. Go to <https://www.notion.so/profile/integrations> → **New integration**.
   - Type: **Internal**, workspace: your team's workspace, name: `Tacit`.
   - Capabilities: **Read content** only. Turn on **Read user information without email
     addresses** too, so pages show authors by name (without it, Tacit uses user ids).
     Tacit never writes to Notion, so leave the update/insert/comment capabilities off.
2. Copy the **Internal Integration Secret** (`ntn_…`).
3. Share the top page(s) you want in Tacit with the integration: open the page →
   **•••** → **Connections** → **Tacit**. Every page below it is shared too.
4. Share the secret with the team the same way you share the graph's database password
   (a password manager). Anyone holding it can read every page shared with the
   integration, which is the same trust model as the shared graph (design §6.3).

## Each teammate

```sh
tacit auth notion                       # paste the ntn_… secret
tacit scope add notion:https://www.notion.so/acme/Eng-Handbook-1a2b3c4d5e6f4a1b8c9d0e1f2a3b4c5d
tacit sync
```

`scope add` accepts a page URL or its id (with or without dashes). A scope entry covers
the page and its whole subtree: child pages, pages in columns, and rows of databases
inside it. Each page becomes one searchable document that cites the page's URL.

## Notes

- **"wasn't found or isn't shared with the integration"**: share the page with the
  integration (step 3), or check you pasted the right team's secret.
- **Unsharing** the top page makes `tacit sync` skip that scope entry with a message;
  pages already synced stay until you `tacit forget <page url>` them. Trashed pages are
  skipped from then on but, likewise, not deleted from the graph.
- The first sync looks back `backfill_days` (default 365, in `config.toml` under
  `[sources]`); later syncs only fetch pages edited since the last one.
- Short pages (under ~200 characters) are indexed for search but skipped by LLM
  extraction.
