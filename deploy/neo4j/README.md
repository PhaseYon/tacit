# Self-hosting the team graph

Most teams should start with **Neo4j Aura Free** (managed, $0, no server to run; see
[docs/quickstart.md](../../docs/quickstart.md)). Use this folder if you want the graph on
your own always-on machine instead: a small VPS, a home server, or an office box.

A laptop is only fine for solo use: teammates can't reach it while it sleeps.

## 1. Start it

```bash
cp .env.example .env          # set a long random NEO4J_PASSWORD
mkdir -p certs backups
# put TLS certs in ./certs (step 2) before starting
docker compose up -d
```

## 2. TLS for Bolt

Teammates connect over the internet, so Bolt must be encrypted. With a domain pointing at
the box, get a Let's Encrypt certificate (e.g. `certbot certonly --standalone -d graph.example.com`)
and copy it in:

```bash
cp /etc/letsencrypt/live/graph.example.com/privkey.pem   certs/private.key
cp /etc/letsencrypt/live/graph.example.com/fullchain.pem certs/public.crt
docker compose restart neo4j
```

Renewals: repeat the copy + restart after `certbot renew` (a cron or deploy hook).
Teammates then use `neo4j+s://graph.example.com:7687` in `tacit init`.

## 3. Firewall

Open only 7687 (Bolt). The Neo4j Browser (7474) is bound to localhost; reach it with
`ssh -L 7474:localhost:7474 you@box` when you need it.

## 4. Backups

Neo4j Community can only dump a stopped database:

```bash
docker compose stop neo4j
docker compose run --rm neo4j neo4j-admin database dump neo4j --to-path=/backups
docker compose start neo4j
```

Run that nightly from cron, or use your VPS provider's volume snapshots. Everything in the
graph can also be rebuilt by re-syncing (`tacit sync` re-fetches from the sources; LLM
extraction would be paid again), so backups mostly protect the access log and saved
extraction spend.

## Access

One password per graph: everyone who has it can read the whole team graph (design doc §1,
shared responsibility). Rotate it when someone leaves:

```bash
docker compose exec neo4j cypher-shell -u neo4j -p "$OLD" \
  "ALTER CURRENT USER SET PASSWORD FROM '$OLD' TO '$NEW'"
```

then update `.env` and tell teammates to re-run `tacit init`.
