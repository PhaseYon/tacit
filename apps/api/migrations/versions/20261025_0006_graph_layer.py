"""Graph layer bookkeeping (docs/design/graph-layer.md §7.1).

Graph data lives in Neo4j (Graphiti); Postgres holds what the graph can't enforce:
which episodes exist, which documents they came from, who may see them, and spend.
graph_episode carries the same RESTRICTIVE source-permission policy as documents, so a
fact is only shown if Postgres agrees the user can read a source it came from.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

from migrations.rls import enable_workspace_rls

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE person (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  display_name text,
  avatar_url text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id)
);
ALTER TABLE external_identity
  ADD CONSTRAINT external_identity_person_fk FOREIGN KEY (workspace_id, person_id)
  REFERENCES person(workspace_id, id) ON DELETE SET NULL (person_id);

CREATE TABLE graph_episode (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id text NOT NULL,
  container_id uuid NOT NULL,
  root_document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
  part integer NOT NULL DEFAULT 0,
  group_id text NOT NULL,
  episode_uuid text,
  input_hash bytea,
  embedding_model text,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'done', 'stale', 'failed', 'skipped_budget')),
  reference_time timestamptz,
  token_estimate integer NOT NULL DEFAULT 0,
  attempts integer NOT NULL DEFAULT 0,
  error text,
  processed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (root_document_id, part),
  FOREIGN KEY (workspace_id, container_id)
    REFERENCES source_container(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX graph_episode_queue_idx ON graph_episode (workspace_id, container_id, status, reference_time);
CREATE INDEX graph_episode_uuid_idx ON graph_episode (episode_uuid);

CREATE TABLE graph_episode_document (
  graph_episode_id uuid NOT NULL REFERENCES graph_episode(id) ON DELETE CASCADE,
  document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
  workspace_id text NOT NULL,
  container_id uuid NOT NULL,
  PRIMARY KEY (graph_episode_id, document_id)
);
CREATE INDEX graph_episode_document_doc_idx ON graph_episode_document (document_id);

CREATE TABLE graph_entity_link (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id text NOT NULL,
  container_id uuid NOT NULL,
  group_id text NOT NULL,
  node_uuid text NOT NULL,
  entity_type text NOT NULL,
  external_identity_id uuid REFERENCES external_identity(id) ON DELETE CASCADE,
  person_id uuid,
  method text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (group_id, node_uuid)
);
CREATE INDEX graph_entity_link_person_idx ON graph_entity_link (workspace_id, person_id);

CREATE TABLE llm_usage (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  container_id uuid,
  graph_episode_id uuid REFERENCES graph_episode(id) ON DELETE SET NULL,
  purpose text NOT NULL CHECK (purpose IN ('graph_extract', 'graph_rebuild', 'agent')),
  provider text NOT NULL,
  model text NOT NULL,
  prompt_name text,
  input_tokens integer NOT NULL,
  cached_input_tokens integer NOT NULL DEFAULT 0,
  output_tokens integer NOT NULL,
  cost_usd numeric(12, 6) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_usage_month_idx ON llm_usage (workspace_id, created_at);

CREATE TABLE workspace_graph_settings (
  workspace_id text PRIMARY KEY REFERENCES organization(id) ON DELETE CASCADE,
  enabled boolean NOT NULL DEFAULT false,
  include_private boolean NOT NULL DEFAULT false,
  monthly_budget_usd numeric(10, 2) NOT NULL DEFAULT 5,
  paused_reason text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""

FUNCTIONS = """
-- Containers the current user may read in the current workspace. SECURITY DEFINER
-- bypasses RLS, so it re-checks membership itself (no separate workspace policy here).
CREATE FUNCTION app.readable_container_ids() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT c.id FROM source_container c
  WHERE c.workspace_id = app.current_workspace_id()
    AND app.is_member(c.workspace_id)
    AND app.can_read_container(c.id)
$$;
REVOKE ALL ON FUNCTION app.readable_container_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.readable_container_ids() TO tacit_api;

-- Workspaces where graph extraction is switched on (for the scheduled scan).
CREATE FUNCTION app.workspaces_graph_enabled() RETURNS SETOF text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT workspace_id FROM workspace_graph_settings WHERE enabled AND paused_reason IS NULL
$$;
REVOKE ALL ON FUNCTION app.workspaces_graph_enabled() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.workspaces_graph_enabled() TO tacit_worker;
"""

ACL_TABLES = ["graph_episode", "graph_episode_document", "graph_entity_link"]
OTHER_TABLES = ["person", "llm_usage", "workspace_graph_settings"]


def upgrade() -> None:
    op.execute(TABLES)
    op.execute(FUNCTIONS)
    for t in ACL_TABLES + OTHER_TABLES:
        enable_workspace_rls(t)
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{t}" TO tacit_worker')
        op.execute(f'GRANT SELECT ON "{t}" TO tacit_api')
    for t in ACL_TABLES:
        op.execute(
            f'CREATE POLICY "{t}_container_acl" ON "{t}" AS RESTRICTIVE FOR SELECT TO tacit_api'
            " USING (app.can_read_container(container_id))"
        )
    # Admins change graph settings from the web app (role checked in the API).
    op.execute("GRANT INSERT, UPDATE ON workspace_graph_settings TO tacit_api")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.workspaces_graph_enabled()")
    op.execute("DROP FUNCTION IF EXISTS app.readable_container_ids()")
    for t in reversed(ACL_TABLES + OTHER_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    op.execute(
        "ALTER TABLE external_identity DROP CONSTRAINT IF EXISTS external_identity_person_fk"
    )
