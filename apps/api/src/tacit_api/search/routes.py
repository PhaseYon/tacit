from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..deps import CurrentWorkspace
from .hybrid import hybrid_search

router = APIRouter(tags=["search"])


class SearchResult(BaseModel):
    document_id: str
    chunk_id: str
    score: float
    matched_by: list[str]
    kind: str
    title: str | None
    url: str
    author: str | None
    created_at: datetime | None
    state: str | None
    container: str
    snippet: str


class SearchOut(BaseModel):
    query: str
    results: list[SearchResult]


@router.get("/workspaces/{workspace_id}/search")
async def search(
    ws: CurrentWorkspace,
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(10, ge=1, le=50),
) -> SearchOut:
    hits = await hybrid_search(ws.db, q, limit)
    return SearchOut(query=q, results=[SearchResult(**asdict(h)) for h in hits])
