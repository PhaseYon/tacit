"""Reusable row-level security helpers for migrations.

Every workspace-scoped table gets enable_workspace_rls(), so tacit_api can only see
rows in the workspace selected for the current transaction, and only when the
current user is a member of it.
"""

from alembic import op


def enable_workspace_rls(table: str, column: str = "workspace_id") -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(
        f"""
        CREATE POLICY "{table}_workspace_isolation" ON "{table}"
        FOR ALL TO tacit_api
        USING ("{column}" = app.current_workspace_id() AND app.is_member("{column}"))
        WITH CHECK ("{column}" = app.current_workspace_id() AND app.is_member("{column}"))
        """
    )


def disable_workspace_rls(table: str) -> None:
    op.execute(f'DROP POLICY IF EXISTS "{table}_workspace_isolation" ON "{table}"')
    op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
