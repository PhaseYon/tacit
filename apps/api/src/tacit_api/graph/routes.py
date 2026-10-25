from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

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
