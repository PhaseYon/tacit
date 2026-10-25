from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..config import get_settings
from ..deps import CurrentWorkspace
from .access import search_facts
from .client import graph_reader

router = APIRouter(tags=["graph"])


class CitationOut(BaseModel):
    url: str
    title: str | None
    kind: str
    container: str


class FactOut(BaseModel):
    fact: str
    relation: str
    status: str
    valid_at: datetime | None
    invalid_at: datetime | None
    citations: list[CitationOut]


@router.get("/workspaces/{workspace_id}/facts")
async def facts(
    ws: CurrentWorkspace,
    q: str = Query(min_length=1, max_length=500),
    history: bool = False,
    limit: int = Query(10, ge=1, le=50),
) -> list[FactOut]:
    graph = graph_reader()
    if graph is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Knowledge graph is not enabled")
    found = await search_facts(ws.db, graph, q, include_history=history, limit=limit)
    return [
        FactOut(
            fact=f.fact,
            relation=f.relation,
            status=f.status,
            valid_at=f.valid_at,
            invalid_at=f.invalid_at,
            citations=[CitationOut(**c.__dict__) for c in f.citations],
        )
        for f in found
    ]


class GraphSettingsIn(BaseModel):
    enabled: bool
    include_private: bool = False
    monthly_budget_usd: float = Field(5, ge=0, le=1000)


class GraphSettingsOut(GraphSettingsIn):
    server_enabled: bool
    paused_reason: str | None
    spent_this_month_usd: float
    episodes: dict[str, int]


async def _read_settings(ws) -> GraphSettingsOut:
    row = (
        await ws.db.execute(
            text(
                "SELECT enabled, include_private, monthly_budget_usd, paused_reason"
                " FROM workspace_graph_settings WHERE workspace_id = :ws"
            ),
            {"ws": ws.workspace_id},
        )
    ).one_or_none()
    spent = await ws.db.scalar(
        text(
            "SELECT COALESCE(sum(cost_usd), 0) FROM llm_usage WHERE workspace_id = :ws"
            " AND created_at >= date_trunc('month', now())"
        ),
        {"ws": ws.workspace_id},
    )
    counts = dict(
        (
            await ws.db.execute(text("SELECT status, count(*) FROM graph_episode GROUP BY status"))
        ).all()
    )
    return GraphSettingsOut(
        enabled=bool(row and row.enabled),
        include_private=bool(row and row.include_private),
        monthly_budget_usd=float(row.monthly_budget_usd) if row else 5.0,
        paused_reason=row.paused_reason if row else None,
        server_enabled=get_settings().graph_enabled,
        spent_this_month_usd=float(spent),
        episodes={k: int(v) for k, v in counts.items()},
    )


@router.get("/workspaces/{workspace_id}/graph/settings")
async def get_graph_settings(ws: CurrentWorkspace) -> GraphSettingsOut:
    return await _read_settings(ws)


@router.put("/workspaces/{workspace_id}/graph/settings")
async def put_graph_settings(ws: CurrentWorkspace, body: GraphSettingsIn) -> GraphSettingsOut:
    if ws.role not in ("owner", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace admins can change this")
    await ws.db.execute(
        text(
            "INSERT INTO workspace_graph_settings (workspace_id, enabled, include_private,"
            " monthly_budget_usd, paused_reason) VALUES (:ws, :en, :priv, :budget, NULL)"
            " ON CONFLICT (workspace_id) DO UPDATE SET enabled = excluded.enabled,"
            " include_private = excluded.include_private,"
            " monthly_budget_usd = excluded.monthly_budget_usd, paused_reason = NULL,"
            " updated_at = now()"
        ),
        {
            "ws": ws.workspace_id,
            "en": body.enabled,
            "priv": body.include_private,
            "budget": body.monthly_budget_usd,
        },
    )
    return await _read_settings(ws)
