"""GitHub connector: installations, repos, source documents, identities, ACLs, webhooks.

See docs/design/github-connector.md section 4. Key rules:
- tacit_worker writes everything, scoped to one workspace per transaction.
- tacit_api is read-only, and a RESTRICTIVE policy on source_document means users only
  see documents from containers they can read on the source (or public ones).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

from migrations.rls import enable_workspace_rls

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Access data older than this is treated as unknown, so private content is hidden.
ACL_MAX_AGE = "6 hours"

TABLES = """
CREATE TABLE github_installation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  installation_id bigint NOT NULL UNIQUE,
  account_id bigint NOT NULL,
  account_login text NOT NULL,
  account_type text NOT NULL,
  repository_selection text,
  permissions jsonb NOT NULL DEFAULT '{}',
  events jsonb NOT NULL DEFAULT '[]',
  installed_by_user_id text REFERENCES "user"(id) ON DELETE SET NULL,
  suspended_at timestamptz,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX github_installation_workspace_idx ON github_installation (workspace_id);

CREATE TABLE source_container (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  source text NOT NULL,
  external_id text NOT NULL,
  name text NOT NULL,
  url text,
  visibility text NOT NULL CHECK (visibility IN ('public', 'private', 'internal')),
  is_active boolean NOT NULL DEFAULT true,
  acl_synced_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, source, external_id),
  UNIQUE (workspace_id, id)
);

CREATE TABLE github_repository (
  container_id uuid PRIMARY KEY,
  workspace_id text NOT NULL,
  installation_id bigint NOT NULL REFERENCES github_installation(installation_id) ON DELETE CASCADE,
  repo_id bigint NOT NULL,
  node_id text NOT NULL,
  full_name text NOT NULL,
  default_branch text NOT NULL,
  archived boolean NOT NULL DEFAULT false,
  sync_enabled boolean NOT NULL DEFAULT true,
  doc_globs text[],
  backfill_status text NOT NULL DEFAULT 'pending'
    CHECK (backfill_status IN ('pending', 'running', 'done', 'failed')),
  last_reconciled_at timestamptz,
  UNIQUE (workspace_id, repo_id),
  FOREIGN KEY (workspace_id, container_id)
    REFERENCES source_container(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE github_sync_cursor (
  container_id uuid NOT NULL,
  workspace_id text NOT NULL,
  resource text NOT NULL,
  since timestamptz,
  next_url text,
  etag text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (container_id, resource),
  FOREIGN KEY (workspace_id, container_id)
    REFERENCES source_container(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE source_document (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id text NOT NULL,
  container_id uuid NOT NULL,
  source text NOT NULL,
  kind text NOT NULL,
  external_id text NOT NULL,
  parent_id uuid REFERENCES source_document(id) ON DELETE SET NULL,
  url text NOT NULL,
  title text,
  body text NOT NULL DEFAULT '',
  author_external_id text,
  author_login text,
  source_created_at timestamptz,
  source_updated_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}',
  content_hash bytea NOT NULL,
  processed_hash bytea,
  token_count integer NOT NULL DEFAULT 0,
  deleted_at timestamptz,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, source, external_id),
  FOREIGN KEY (workspace_id, container_id)
    REFERENCES source_container(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX source_document_container_idx ON source_document (workspace_id, container_id);
CREATE INDEX source_document_parent_idx ON source_document (parent_id);
CREATE INDEX source_document_unprocessed_idx ON source_document (workspace_id)
  WHERE processed_hash IS DISTINCT FROM content_hash AND deleted_at IS NULL;

CREATE TABLE external_identity (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id text NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  source text NOT NULL,
  external_id text NOT NULL,
  login text,
  display_name text,
  avatar_url text,
  is_bot boolean NOT NULL DEFAULT false,
  person_id uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, source, external_id)
);

CREATE TABLE container_access (
  container_id uuid NOT NULL,
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  workspace_id text NOT NULL,
  permission text NOT NULL,
  via_external_id text,
  synced_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (container_id, user_id),
  FOREIGN KEY (workspace_id, container_id)
    REFERENCES source_container(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX container_access_user_idx ON container_access (user_id);

-- Intake log for GitHub webhooks. Not workspace-scoped (the workspace isn't known
-- until the installation is resolved), so only tacit_worker can touch it.
CREATE TABLE webhook_delivery (
  delivery_id text PRIMARY KEY,
  source text NOT NULL DEFAULT 'github',
  event text NOT NULL,
  action text,
  installation_id bigint,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'done', 'ignored', 'failed')),
  attempts integer NOT NULL DEFAULT 0,
  error text,
  payload jsonb NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz
);
CREATE INDEX webhook_delivery_status_idx ON webhook_delivery (status, received_at);
"""

FUNCTIONS = f"""
-- Worker jobs start from an installation id and need its workspace, without being
-- allowed to read other workspaces' rows.
CREATE FUNCTION app.workspace_for_installation(inst bigint) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT workspace_id FROM github_installation
  WHERE installation_id = inst AND deleted_at IS NULL
$$;

-- The one access rule for synced content. Fails closed: inactive containers, missing
-- or stale ACLs, and users without an access row all get false.
CREATE FUNCTION app.can_read_container(cid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM source_container c
    WHERE c.id = cid
      AND c.is_active
      AND c.workspace_id = app.current_workspace_id()
      AND (
        c.visibility = 'public'
        OR (
          c.acl_synced_at > now() - interval '{ACL_MAX_AGE}'
          AND EXISTS (
            SELECT 1 FROM container_access a
            WHERE a.container_id = c.id AND a.user_id = app.current_user_id()
          )
        )
      )
  )
$$;

REVOKE ALL ON FUNCTION app.workspace_for_installation(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.can_read_container(uuid) FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO tacit_worker;
GRANT EXECUTE ON FUNCTION app.current_user_id(), app.current_workspace_id(),
  app.workspace_for_installation(bigint) TO tacit_worker;
GRANT EXECUTE ON FUNCTION app.can_read_container(uuid) TO tacit_api;
"""

WORKSPACE_TABLES = [
    "github_installation",
    "source_container",
    "github_repository",
    "github_sync_cursor",
    "source_document",
    "external_identity",
    "container_access",
]
API_READABLE = [
    "github_installation",
    "source_container",
    "github_repository",
    "source_document",
    "external_identity",
    "container_access",
]


def upgrade() -> None:
    op.execute(TABLES)
    op.execute(FUNCTIONS)

    for t in WORKSPACE_TABLES:
        enable_workspace_rls(t)
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{t}" TO tacit_worker')
    for t in API_READABLE:
        op.execute(f'GRANT SELECT ON "{t}" TO tacit_api')
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_delivery TO tacit_worker")

    # Users see a document only if they can read its container on the source.
    # RESTRICTIVE: ANDed with the workspace policy, so it can only narrow access.
    op.execute(
        """
        CREATE POLICY source_document_container_acl ON source_document
        AS RESTRICTIVE FOR SELECT TO tacit_api
        USING (app.can_read_container(container_id))
        """
    )

    # ACL sync needs workspace members and their linked GitHub accounts, never tokens.
    op.execute('GRANT SELECT ON "member" TO tacit_worker')
    op.execute(
        'CREATE POLICY "member_worker_read" ON "member" FOR SELECT TO tacit_worker '
        'USING ("organizationId" = app.current_workspace_id())'
    )
    op.execute('GRANT SELECT ("userId", "providerId", "accountId") ON "account" TO tacit_worker')


def downgrade() -> None:
    op.execute('REVOKE SELECT ("userId", "providerId", "accountId") ON "account" FROM tacit_worker')
    op.execute('DROP POLICY IF EXISTS "member_worker_read" ON "member"')
    op.execute('REVOKE SELECT ON "member" FROM tacit_worker')
    for t in ["webhook_delivery", *reversed(WORKSPACE_TABLES)]:
        op.execute(f'DROP TABLE "{t}" CASCADE')
    op.execute("DROP FUNCTION app.can_read_container(uuid)")
    op.execute("DROP FUNCTION app.workspace_for_installation(bigint)")
    op.execute("REVOKE USAGE ON SCHEMA app FROM tacit_worker")
