"""Reusable row-level security helpers for migrations.

Every workspace-scoped table gets enable_workspace_rls():
- tacit_api sees rows only in the workspace selected for the current transaction, and
  only when the current user is a member of it.
- tacit_worker (jobs have no user) sees rows only in the workspace the job set.
"""

from alembic import op


def enable_workspace_rls(table: str, column: str = "workspace_id", worker: bool = True) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(
        f"""
        CREATE POLICY "{table}_workspace_isolation" ON "{table}"
        FOR ALL TO tacit_api
        USING ("{column}" = app.current_workspace_id() AND app.is_member("{column}"))
        WITH CHECK ("{column}" = app.current_workspace_id() AND app.is_member("{column}"))
        """
    )
    if worker:
        op.execute(
            f"""
            CREATE POLICY "{table}_worker_workspace" ON "{table}"
            FOR ALL TO tacit_worker
            USING ("{column}" = app.current_workspace_id())
            WITH CHECK ("{column}" = app.current_workspace_id())
            """
        )


def disable_workspace_rls(table: str) -> None:
    op.execute(f'DROP POLICY IF EXISTS "{table}_workspace_isolation" ON "{table}"')
    op.execute(f'DROP POLICY IF EXISTS "{table}_worker_workspace" ON "{table}"')
    op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
