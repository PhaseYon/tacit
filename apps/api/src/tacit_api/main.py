from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select

from .config import get_settings
from .db import dispose_engine
from .deps import CurrentPrincipal, CurrentWorkspace, UserDB
from .models import Member, User, Workspace


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await dispose_engine()


app = FastAPI(title="Tacit API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().web_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class WorkspaceOut(BaseModel):
    id: str
    name: str
    slug: str
    role: str


class MeOut(BaseModel):
    id: str
    email: str | None
    name: str | None
    workspaces: list[WorkspaceOut]


class MemberOut(BaseModel):
    user_id: str
    name: str
    email: str
    role: str
    joined_at: datetime


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/me")
async def me(principal: CurrentPrincipal, db: UserDB) -> MeOut:
    rows = await db.execute(
        select(Workspace, Member.role)
        .join(Member, Member.workspace_id == Workspace.id)
        .where(Member.user_id == principal.user_id)
        .order_by(Workspace.name)
    )
    return MeOut(
        id=principal.user_id,
        email=principal.email,
        name=principal.name,
        workspaces=[WorkspaceOut(id=w.id, name=w.name, slug=w.slug, role=role) for w, role in rows],
    )


@app.get("/workspaces/{workspace_id}/members")
async def list_members(ws: CurrentWorkspace) -> list[MemberOut]:
    rows = await ws.db.execute(
        select(Member, User)
        .join(User, User.id == Member.user_id)
        .where(Member.workspace_id == ws.workspace_id)
        .order_by(Member.created_at)
    )
    return [
        MemberOut(user_id=u.id, name=u.name, email=u.email, role=m.role, joined_at=m.created_at)
        for m, u in rows
    ]


def run() -> None:
    import uvicorn

    uvicorn.run("tacit_api.main:app", host="0.0.0.0", port=8000, reload=True)
