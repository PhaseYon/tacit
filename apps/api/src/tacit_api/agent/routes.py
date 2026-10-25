from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..db import worker_session
from ..deps import CurrentWorkspace
from ..graph.costs import BudgetExceeded
from ..llm import LLMNotConfigured, get_provider
from .ask import AskContext, ask, record_agent_usage

router = APIRouter(tags=["ask"])


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class SourceOut(BaseModel):
    n: int
    kind: str
    title: str | None
    url: str
    text: str
    meta: str
    cited: bool


class AskOut(BaseModel):
    mode: str  # generated | sources_only
    answer: str
    insufficient: bool
    sources: list[SourceOut]
    cost_usd: float
    note: str | None = None


async def _budget_remaining(ws) -> Decimal:
    budget = await ws.db.scalar(
        text("SELECT monthly_budget_usd FROM workspace_graph_settings WHERE workspace_id = :ws"),
        {"ws": ws.workspace_id},
    )
    spent = await ws.db.scalar(
        text(
            "SELECT COALESCE(sum(cost_usd), 0) FROM llm_usage WHERE workspace_id = :ws"
            " AND created_at >= date_trunc('month', now())"
        ),
        {"ws": ws.workspace_id},
    )
    return Decimal(budget if budget is not None else 5) - Decimal(spent)


@router.post("/workspaces/{workspace_id}/ask")
async def ask_endpoint(ws: CurrentWorkspace, body: AskIn) -> AskOut:
    from ..graph.client import graph_reader

    note = None
    try:
        provider = get_provider()
    except LLMNotConfigured:
        provider, note = None, "No LLM configured: showing the most relevant sources only."
    remaining = await _budget_remaining(ws)
    if provider is not None and remaining <= 0:
        provider, note = None, "This month's LLM budget is used up: showing sources only."
    ctx = AskContext(ws.db, provider, graph_reader(), ws.workspace_id, remaining)
    try:
        state = await ask(body.question, ctx)
    except BudgetExceeded:
        ctx.provider, note = None, "This month's LLM budget is used up: showing sources only."
        state = await ask(body.question, ctx)
    await record_agent_usage(worker_session, ws.workspace_id, ctx.usage)
    cited = set(state.get("cited") or [])
    generated = ctx.provider is not None and bool(state.get("answer"))
    return AskOut(
        mode="generated" if generated else "sources_only",
        answer=state.get("answer") or "",
        insufficient=bool(state.get("insufficient")),
        sources=[SourceOut(**s.__dict__, cited=s.n in cited) for s in state.get("sources") or []],
        cost_usd=float(sum(u.cost_usd for u in ctx.usage)),
        note=note,
    )
