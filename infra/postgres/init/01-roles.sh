#!/bin/bash
# Runs once, when the Postgres volume is first created.
#
# Three roles so row-level security (RLS) actually protects data:
#   tacit_migrator  owns every table and runs Alembic. Owners bypass RLS, so apps never use it.
#   tacit_auth      Better Auth (Next.js). Gets grants on auth tables only, never knowledge data.
#   tacit_api       FastAPI, workers and MCP. RLS is always enforced for it.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v migrator_pw="$TACIT_MIGRATOR_PASSWORD" \
  -v auth_pw="$TACIT_AUTH_PASSWORD" \
  -v api_pw="$TACIT_API_PASSWORD" \
  -v db="$POSTGRES_DB" <<'SQL'
CREATE ROLE tacit_migrator LOGIN PASSWORD :'migrator_pw';
CREATE ROLE tacit_auth     LOGIN PASSWORD :'auth_pw'     NOBYPASSRLS;
CREATE ROLE tacit_api      LOGIN PASSWORD :'api_pw'      NOBYPASSRLS;

REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO tacit_migrator, tacit_auth, tacit_api;
GRANT CREATE ON DATABASE :"db" TO tacit_migrator;

-- Extensions need a superuser, so create them here rather than in migrations.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO tacit_migrator;
GRANT USAGE ON SCHEMA public TO tacit_auth, tacit_api;
SQL
