"""Database access as the tacit_api role, which always has RLS enforced.

Every request runs in one transaction. The user and workspace are set with
set_config(..., is_local=true), so they reset at commit and can't leak into the
next request that reuses the pooled connection.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(get_settings().api_database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = _sessionmaker = None


async def set_rls_context(
    session: AsyncSession, *, user_id: str, workspace_id: str | None = None
) -> None:
    await session.execute(
        text(
            "SELECT set_config('app.user_id', :uid, true),"
            " set_config('app.workspace_id', :wid, true)"
        ),
        {"uid": user_id, "wid": workspace_id or ""},
    )


@asynccontextmanager
async def rls_session(user_id: str, workspace_id: str | None = None) -> AsyncIterator[AsyncSession]:
    """A transaction scoped to one user (and optionally one workspace)."""
    get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session, session.begin():
        await set_rls_context(session, user_id=user_id, workspace_id=workspace_id)
        yield session
