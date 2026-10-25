# Slack app setup (dev)

One Slack app does both jobs: the **bot** reads the channels you invite it to, and
**Sign in with Slack** (OpenID Connect) links each person's Slack account, which is what
permission sync uses. Design: [docs/design/slack-connector.md](../../docs/design/slack-connector.md).

1. Use a **sandbox Slack workspace** (never a real team's Slack on a dev instance).
2. Slack needs public HTTPS URLs for events and OAuth redirects. Start tunnels:
   `cloudflared tunnel --url http://localhost:8000` (API) and
   `cloudflared tunnel --url http://localhost:3000` (web).
3. At <https://api.slack.com/apps> choose **Create New App → From a manifest**, paste
   [`app-manifest.json`](app-manifest.json) and replace the `REPLACE-WITH-…` hosts with
   your tunnels.
4. From **Basic Information** copy into `.env`: `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`,
   `SLACK_SIGNING_SECRET`. Set `API_PUBLIC_URL` to the API tunnel and `BETTER_AUTH_URL`
   to the web tunnel, then restart `pnpm dev:api`, `pnpm dev:web` and `pnpm dev:worker`.
5. In Tacit: open the workspace page → **Link your Slack account** → **Connect Slack**.
   Then, in Slack, `/invite @Tacit` in each channel you want synced.

Scopes are read-only, and there are no DM scopes. Tacit syncs only channels it was
invited to. Messages in a channel are visible in Tacit only to workspace members whose
linked Slack account can read that channel in Slack.
