# Slack app setup

Tacit reads Slack with **each person's own user token** (`xoxp-`), so it can only ever
see what that person can already see in Slack. There is no bot, no webhook and no
events subscription. `tacit sync` polls the channels in the graph's scope.

The app is one your team creates for its own workspace. Slack applies tighter history
limits to apps distributed outside the Marketplace; an app built for your own workspace
avoids them.

## Once per team (one person)

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**, pick
   your workspace and paste [`app-manifest.json`](app-manifest.json).
2. That's it. Share the app's page (or its **Install App** link) with your teammates.

## Each person

1. On the app's page, open **Install App** → **Install to Workspace** and approve. You
   are granting the app access as yourself.
2. Copy the **User OAuth Token** (starts with `xoxp-`).
3. Run `tacit auth slack` and paste it. It is stored in your OS keychain.
4. Add channels: `tacit scope add slack:#eng`, then `tacit sync`.

## Scopes

All user scopes, all read-only:

| Scope | Why |
|---|---|
| `channels:history`, `groups:history` | Read messages and threads in public channels and in private channels you're a member of |
| `channels:read`, `groups:read` | Find a channel by name for `tacit scope add` |
| `users:read` | Show `@handle` instead of raw user ids in transcripts |
| `users:read.email` | *Optional.* Lets Tacit link your Slack, GitHub and Notion accounts by email. Remove it from the manifest if you'd rather not. |

There are no DM scopes (`im:*`, `mpim:*`): DMs and group DMs are never syncable.
Private channels need `tacit scope add --private` because everyone with the graph's
password will be able to read what's synced from them.

If your token can't read a channel in scope (you're not in a private channel, say), your
`tacit sync` skips it and a teammate who can read it picks it up.
