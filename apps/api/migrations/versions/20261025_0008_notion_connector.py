"""Notion connector: personal connections (encrypted tokens), page trees, bookkeeping.

See docs/design/notion-connector.md sections 1 and 4. Reuses source_container /
source_document / container_access / external_identity / webhook_delivery. Key rules:
- A connection belongs to one Tacit user. Its containers (page trees) are always
  visibility 'private'; access rows are the owner, plus all workspace members only for
  trees an admin-owner explicitly marked workspace_visible.
- tacit_api reads notion_connection through a column grant that omits both encrypted
  tokens, so the API role can't even read the ciphertext.
- tacit_worker writes everything, scoped to one workspace per transaction.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

from migrations.rls import enable_workspace_rls

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE notion_connection (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  notion_workspace_id text NOT NULL,
  notion_workspace_name text,
  notion_user_id text NOT NULL,
  notion_user_name text,
  bot_id text NOT NULL UNIQUE,
  access_token_encrypted bytea,
  refresh_token_encrypted bytea,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, user_id, notion_workspace_id),
  UNIQUE (workspace_id, id),
  -- A live connection always has a token; revoking wipes it.
  CHECK (revoked_at IS NOT NULL OR access_token_encrypted IS NOT NULL)
);
CREATE INDEX notion_connection_notion_ws_idx ON notion_connection (notion_workspace_id);

CREATE TABLE notion_tree (
  container_id uuid PRIMARY KEY,
  workspace_id text NOT NULL,
  connection_id uuid NOT NULL,
  root_id text NOT NULL,
  root_type text NOT NULL CHECK (root_type IN ('page', 'data_source')),
  title text NOT NULL,
  workspace_visible boolean NOT NULL DEFAULT false,
  marked_by_user_id text REFERENCES "user"(id) ON DELETE SET NULL,
  marked_at timestamptz,
  UNIQUE (connection_id, root_id),
  FOREIGN KEY (workspace_id, connection_id)
    REFERENCES notion_connection(workspace_id, id) ON DELETE CASCADE,
  FOREIGN KEY (workspace_id, container_id)
    REFERENCES source_container(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE notion_page (
  connection_id uuid NOT NULL,
  page_id text NOT NULL,
  workspace_id text NOT NULL,
  object text NOT NULL CHECK (object IN ('page', 'data_source')),
  database_id text,
  title text,
  url text,
  parent_type text,
  parent_id text,
  container_id uuid,
  last_edited_time timestamptz,
  fetched_at timestamptz,
  seen_run uuid,
  PRIMARY KEY (connection_id, page_id),
  FOREIGN KEY (workspace_id, connection_id)
    REFERENCES notion_connection(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE notion_sync_cursor (
  connection_id uuid PRIMARY KEY,
  workspace_id text NOT NULL,
  mode text CHECK (mode IN ('full', 'incremental')),
  next_cursor text,
  run_id uuid,
  run_started_at timestamptz,
  run_max_edited timestamptz,
  watermark timestamptz,
  last_full_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, connection_id)
    REFERENCES notion_connection(workspace_id, id) ON DELETE CASCADE
);
"""

FUNCTIONS = """
-- Webhooks carry the Notion workspace id; several connections (people, even Tacit
-- workspaces) can share one. Returns ids only, never tokens.
CREATE FUNCTION app.notion_connections_for(notion_ws text)
RETURNS TABLE (workspace_id text, connection_id uuid, bot_id text, notion_user_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT workspace_id, id, bot_id, notion_user_id FROM notion_connection
  WHERE notion_workspace_id = notion_ws AND revoked_at IS NULL
$$;

CREATE FUNCTION app.list_notion_connections()
RETURNS TABLE (workspace_id text, connection_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT workspace_id, id FROM notion_connection WHERE revoked_at IS NULL
$$;

REVOKE ALL ON FUNCTION app.notion_connections_for(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.list_notion_connections() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.notion_connections_for(text),
  app.list_notion_connections() TO tacit_worker;
"""

WORKSPACE_TABLES = ["notion_connection", "notion_tree", "notion_page", "notion_sync_cursor"]
# Everything except the two encrypted token columns.
CONNECTION_API_COLUMNS = (
    "id, workspace_id, user_id, notion_workspace_id, notion_workspace_name, notion_user_id,"
    " notion_user_name, bot_id, revoked_at, created_at, updated_at"
)


def upgrade() -> None:
    op.execute(TABLES)
    op.execute(FUNCTIONS)
    for t in WORKSPACE_TABLES:
        enable_workspace_rls(t)
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{t}" TO tacit_worker')
    op.execute(f"GRANT SELECT ({CONNECTION_API_COLUMNS}) ON notion_connection TO tacit_api")
    op.execute("GRANT SELECT ON notion_tree TO tacit_api")


def downgrade() -> None:
    for t in reversed(WORKSPACE_TABLES):
        op.execute(f'DROP TABLE "{t}" CASCADE')
    op.execute("DROP FUNCTION app.list_notion_connections()")
    op.execute("DROP FUNCTION app.notion_connections_for(text)")
