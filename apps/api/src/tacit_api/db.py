"""Database access. Two roles, both with RLS always enforced:

- tacit_api (rls_session): web/MCP requests, scoped to one user and optionally one workspace.
- tacit_worker (worker_session): jobs and webhook intake, scoped to one workspace.

Every unit of work runs in one transaction. Context is set with
set_config(..., is_local=true), so it resets at commit and can't leak into the next
transaction that reuses a pooled connection.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings

_engines: dict[str, AsyncEngine] = {}
_makers: dict[str, async_sessionmaker[AsyncSession]] = {}


def _maker(kind: str) -> async_sessionmaker[AsyncSession]:
    if kind not in _makers:
        s = get_settings()
        if kind == "api":
            engine = create_async_engine(s.api_database_url, pool_pre_ping=True)
        else:
            # Celery runs each task in a fresh event loop (asyncio.run), and asyncpg
            # connections can't move between loops, so workers don't pool.
            engine = create_async_engine(s.worker_database_url, poolclass=NullPool)
        _engines[kind] = engine
        _makers[kind] = async_sessionmaker(engine, expire_on_commit=False)
    return _makers[kind]


def get_engine() -> AsyncEngine:
    _maker("api")
    return _engines["api"]


async def dispose_engine() -> None:
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()
    _makers.clear()


async def set_rls_context(
    session: AsyncSession, *, user_id: str | None = None, workspace_id: str | None = None
) -> None:
    await session.execute(
        text(
            "SELECT set_config('app.user_id', :uid, true),"
            " set_config('app.workspace_id', :wid, true)"
        ),
        {"uid": user_id or "", "wid": workspace_id or ""},
    )


@asynccontextmanager
async def rls_session(user_id: str, workspace_id: str | None = None) -> AsyncIterator[AsyncSession]:
    """A tacit_api transaction scoped to one user (and optionally one workspace)."""
    async with _maker("api")() as session, session.begin():
        await set_rls_context(session, user_id=user_id, workspace_id=workspace_id)
        yield session


@asynccontextmanager
async def worker_session(workspace_id: str | None = None) -> AsyncIterator[AsyncSession]:
    """A tacit_worker transaction scoped to one workspace.

    Without a workspace it can only touch webhook_delivery and resolve installations.
    """
    async with _maker("worker")() as session, session.begin():
        await set_rls_context(session, workspace_id=workspace_id)
        yield session
