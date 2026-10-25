# GitHub App setup (dev)

Tacit syncs GitHub through a **GitHub App**. The same App also handles "Sign in / Link
GitHub", which gives us each user's verified GitHub id for permission sync.

## 1. Create the App (about a minute)

```bash
pnpm github:create-app            # under your account
pnpm github:create-app --org ORG  # or under an org
```

The script:
1. Creates a [smee.io](https://smee.io) channel so GitHub can deliver webhooks to localhost.
2. Opens a page that submits [`app-manifest.json`](app-manifest.json) to GitHub. Click
   **Create GitHub App**.
3. Saves the private key to `~/.config/tacit/` (outside the repo) and writes the App's id,
   slug, OAuth client and webhook secret into `.env`.

The App is created as **private** (installable only on the account that owns it) with
read-only permissions: metadata, issues, pull requests, contents, org members, and the
user's email addresses.

## 2. Run everything

```bash
pnpm infra:up && pnpm db:migrate
pnpm dev:api        # restart it after creating the App so it reads the new .env
pnpm dev:web
pnpm dev:worker     # Celery worker + beat (sync jobs, hourly ACL refresh, reconcile)
pnpm dev:webhooks   # relays GitHub webhooks from smee.io to localhost:8000
```

## 3. Connect a repo

1. In your workspace, click **Link your GitHub account**.
2. Click **Connect GitHub repositories**, pick repos on GitHub, and you're sent back.
3. Repos appear and backfill in the background. You see content only from repos your
   GitHub account can access (public repos are visible to all workspace members).

Only install the dev App on repos you own, like this repo or a sandbox. Never install it
on real private team data (see CLAUDE.md).

## Troubleshooting

- **Webhook signature errors:** if a relay re-serializes bodies, use
  `cloudflared tunnel --url http://localhost:8000` and set the App's webhook URL to it.
- **Missed events:** the 6-hourly reconcile catches up, or redeliver from the App's
  *Advanced* settings page.
- **"not_allowed" after installing:** org installs need you to be an org admin. Personal
  installs must be on the GitHub account you linked.
