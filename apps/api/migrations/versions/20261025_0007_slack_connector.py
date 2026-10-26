"""Slack connector: installations (encrypted bot token), channels, sync cursors.

See docs/design/slack-connector.md section 4. Reuses source_container / source_document /
container_access / external_identity / webhook_delivery from 0002. Key rules:
- tacit_worker writes everything, scoped to one workspace per transaction.
- tacit_api reads slack_installation through a column grant that omits the encrypted
  bot token, so the API role can't even read the ciphertext.
- Slack public channels are stored as visibility 'internal' (an access row is required),
  never 'public', because they're public only inside the Slack team.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

from migrations.rls import enable_workspace_rls

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE slack_installation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  team_id text NOT NULL UNIQUE,
  team_name text NOT NULL,
  team_domain text,
  enterprise_id text,
  app_id text,
  bot_user_id text NOT NULL,
  scopes text[] NOT NULL DEFAULT '{}',
  bot_token_encrypted bytea,
  installed_by_user_id text REFERENCES "user"(id) ON DELETE SET NULL,
  installed_by_slack_user_id text,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  -- A live installation always has a token; revoking wipes it.
  CHECK (revoked_at IS NOT NULL OR bot_token_encrypted IS NOT NULL)
);
CREATE INDEX slack_installation_workspace_idx ON slack_installation (workspace_id);

CREATE TABLE slack_channel (
  container_id uuid PRIMARY KEY,
  workspace_id text NOT NULL,
  team_id text NOT NULL REFERENCES slack_installation(team_id) ON DELETE CASCADE,
  channel_id text NOT NULL,
  name text NOT NULL,
  is_private boolean NOT NULL,
  is_archived boolean NOT NULL DEFAULT false,
  sync_enabled boolean NOT NULL DEFAULT true,
  backfill_status text NOT NULL DEFAULT 'pending'
    CHECK (backfill_status IN ('pending', 'running', 'done', 'failed')),
  last_reconciled_at timestamptz,
  UNIQUE (workspace_id, team_id, channel_id),
  FOREIGN KEY (workspace_id, container_id)
    REFERENCES source_container(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE slack_sync_cursor (
  container_id uuid NOT NULL,
  workspace_id text NOT NULL,
  resource text NOT NULL,
  oldest_ts text,
  next_cursor text,
  run_latest_ts text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (container_id, resource),
  FOREIGN KEY (workspace_id, container_id)
    REFERENCES source_container(workspace_id, id) ON DELETE CASCADE
);

ALTER TABLE webhook_delivery ADD COLUMN team_id text;

-- Orphaned replies are relinked to their thread root by thread_ts.
CREATE INDEX source_document_slack_thread_idx ON source_document
  (container_id, (metadata->>'thread_ts')) WHERE source = 'slack';
"""

FUNCTIONS = """
CREATE FUNCTION app.workspace_for_slack_team(team text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT workspace_id FROM slack_installation WHERE team_id = team AND revoked_at IS NULL
$$;

CREATE FUNCTION app.list_slack_installations()
RETURNS TABLE (workspace_id text, team_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT workspace_id, team_id FROM slack_installation WHERE revoked_at IS NULL
$$;

REVOKE ALL ON FUNCTION app.workspace_for_slack_team(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.list_slack_installations() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.workspace_for_slack_team(text),
  app.list_slack_installations() TO tacit_worker;
"""

WORKSPACE_TABLES = ["slack_installation", "slack_channel", "slack_sync_cursor"]
# Everything except bot_token_encrypted.
INSTALLATION_API_COLUMNS = (
    "id, workspace_id, team_id, team_name, team_domain, enterprise_id, app_id, bot_user_id,"
    " scopes, installed_by_user_id, installed_by_slack_user_id, revoked_at, created_at,"
    " updated_at"
)


def upgrade() -> None:
    op.execute(TABLES)
    op.execute(FUNCTIONS)
    for t in WORKSPACE_TABLES:
        enable_workspace_rls(t)
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{t}" TO tacit_worker')
    op.execute(f"GRANT SELECT ({INSTALLATION_API_COLUMNS}) ON slack_installation TO tacit_api")
    op.execute("GRANT SELECT ON slack_channel TO tacit_api")


def downgrade() -> None:
    for t in reversed(WORKSPACE_TABLES):
        op.execute(f'DROP TABLE "{t}" CASCADE')
    op.execute("DROP INDEX IF EXISTS source_document_slack_thread_idx")
    op.execute("ALTER TABLE webhook_delivery DROP COLUMN team_id")
    op.execute("DROP FUNCTION app.list_slack_installations()")
    op.execute("DROP FUNCTION app.workspace_for_slack_team(text)")
