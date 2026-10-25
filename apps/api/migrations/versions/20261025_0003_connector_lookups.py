"""Lookups for scheduled jobs and parent resolution.

- app.list_github_installations(): scheduled jobs (reconcile, ACL refresh) start with
  no workspace selected. This returns only (workspace_id, installation_id) pairs, so the
  worker can then open one workspace-scoped transaction per installation.
- Index to resolve a comment's parent issue/PR by number within a repo.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app.list_github_installations()
        RETURNS TABLE (workspace_id text, installation_id bigint)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
          SELECT workspace_id, installation_id FROM github_installation
          WHERE deleted_at IS NULL AND suspended_at IS NULL
        $$;
        REVOKE ALL ON FUNCTION app.list_github_installations() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app.list_github_installations() TO tacit_worker;
        """
    )
    op.execute(
        "CREATE INDEX source_document_number_idx ON source_document"
        " (container_id, ((metadata->>'number')::int))"
        " WHERE kind IN ('issue', 'pull_request')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX source_document_number_idx")
    op.execute("DROP FUNCTION app.list_github_installations()")
