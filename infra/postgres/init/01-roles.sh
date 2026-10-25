#!/bin/bash
# Creates the app roles. Runs automatically when the Postgres volume is first created,
# and is safe to re-run on an existing database (`pnpm db:roles`).
#
# Separate roles so row-level security (RLS) actually protects data:
#   tacit_migrator  owns every table and runs Alembic. Owners bypass RLS, so apps never use it.
#   tacit_auth      Better Auth (Next.js). Grants on auth tables only, never knowledge data.
#   tacit_api       FastAPI and MCP. RLS always enforced; read-only on synced content.
#   tacit_worker    Celery workers and webhook intake. RLS scoped to one workspace per job.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v migrator_pw="$TACIT_MIGRATOR_PASSWORD" \
  -v auth_pw="$TACIT_AUTH_PASSWORD" \
  -v api_pw="$TACIT_API_PASSWORD" \
  -v worker_pw="${TACIT_WORKER_PASSWORD:-worker}" \
  -v db="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN NOBYPASSRLS', r)
FROM unnest(ARRAY['tacit_migrator', 'tacit_auth', 'tacit_api', 'tacit_worker']) AS r
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)
\gexec

ALTER ROLE tacit_migrator PASSWORD :'migrator_pw';
ALTER ROLE tacit_auth     PASSWORD :'auth_pw';
ALTER ROLE tacit_api      PASSWORD :'api_pw';
ALTER ROLE tacit_worker   PASSWORD :'worker_pw';

REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO tacit_migrator, tacit_auth, tacit_api, tacit_worker;
GRANT CREATE ON DATABASE :"db" TO tacit_migrator;

-- Extensions need a superuser, so create them here rather than in migrations.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO tacit_migrator;
GRANT USAGE ON SCHEMA public TO tacit_auth, tacit_api, tacit_worker;
SQL
