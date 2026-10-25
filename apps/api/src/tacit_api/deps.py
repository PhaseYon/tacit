from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import Principal, verify_token
from .db import rls_session, set_rls_context
from .models import Member

_bearer = HTTPBearer(auto_error=False)


async def current_principal(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if creds is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await verify_token(creds.credentials)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


async def user_db(principal: CurrentPrincipal) -> AsyncIterator[AsyncSession]:
    """DB session that can see only what this user is allowed to see (no workspace selected)."""
    async with rls_session(principal.user_id) as session:
        yield session


UserDB = Annotated[AsyncSession, Depends(user_db)]


@dataclass(frozen=True)
class WorkspaceContext:
    db: AsyncSession
    principal: Principal
    workspace_id: str
    role: str


async def workspace_context(
    workspace_id: str, principal: CurrentPrincipal, db: UserDB
) -> WorkspaceContext:
    """For routes under /workspaces/{workspace_id}. 404s if the user isn't a member.

    The app-level check gives a clean error; RLS enforces the same rule in Postgres.
    """
    role = await db.scalar(
        select(Member.role).where(
            Member.workspace_id == workspace_id, Member.user_id == principal.user_id
        )
    )
    if role is None:
        # 404 rather than 403 so workspace IDs can't be probed.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    await set_rls_context(db, user_id=principal.user_id, workspace_id=workspace_id)
    return WorkspaceContext(db=db, principal=principal, workspace_id=workspace_id, role=role)


CurrentWorkspace = Annotated[WorkspaceContext, Depends(workspace_context)]
