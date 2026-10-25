"""Answer "why did we do X, who decided, and where's the evidence?" with citations.

A small LangGraph workflow (CLAUDE.md: one agent, no more until the benchmark says so):
retrieve (hybrid passages + graph facts, under the caller's RLS) -> answer (one
structured LLM call that must cite numbered sources) -> validate (drop citations to
sources that weren't retrieved). Every LLM call is costed against the workspace budget.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..llm import ChatMessage, LLMProvider, Usage
from ..search.hybrid import hybrid_search


class AnswerSchema(BaseModel):
    answer: str = Field(
        description="Markdown answer. Every factual sentence ends with source markers like [2] or [1][3]."
    )
    insufficient: bool = Field(description="True if the sources don't answer the question")


@dataclass
class Source:
    n: int
    kind: str  # passage | fact
    title: str | None
    url: str
    text: str
    meta: str


@dataclass
class AskContext:
    session: AsyncSession
    provider: LLMProvider | None
    graph: Any | None  # Graphiti read-only instance, or None when the graph is off
    workspace_id: str
    budget_remaining_usd: Decimal
    usage: list[Usage] = field(default_factory=list)


class AskState(TypedDict, total=False):
    question: str
    sources: list[Source]
    answer: str
    insufficient: bool
    cited: list[int]


SYSTEM = """You answer questions about a software team's decisions using ONLY the numbered
sources provided (pull requests, reviews, issues, comments, design docs, and graph facts
with validity dates). Structure the answer as:
- The current decision (what is true now), with sources.
- What it replaced and when, if the sources show a change.
- Who proposed or approved it, if the sources say.
Cite every factual sentence with [n] markers. Never cite a number that isn't listed.
Prefer facts marked "current" over "superseded" when stating what is true now. If the
sources don't answer the question, say so briefly and set insufficient=true. Be concise."""


async def retrieve(state: AskState, ctx: AskContext) -> AskState:
    sources: list[Source] = []
    hits = await hybrid_search(ctx.session, state["question"], limit=8)
    for h in hits:
        date = h.created_at.date().isoformat() if h.created_at else "undated"
        sources.append(
            Source(
                len(sources) + 1,
                "passage",
                h.title,
                h.url,
                h.snippet.replace("**", ""),
                f"{h.kind} in {h.container} by {h.author or 'unknown'}, {date}",
            )
        )
    if ctx.graph is not None:
        from ..graph.access import search_facts

        for f in await search_facts(
            ctx.session, ctx.graph, state["question"], include_history=True, limit=10
        ):
            when = f"valid from {f.valid_at.date() if f.valid_at else '?'}"
            if f.invalid_at:
                when += f" until {f.invalid_at.date()}"
            c = f.citations[0]
            sources.append(
                Source(len(sources) + 1, "fact", c.title, c.url, f.fact, f"{f.status} fact, {when}")
            )
    return {"sources": sources}


async def answer(state: AskState, ctx: AskContext) -> AskState:
    sources = state.get("sources") or []
    if not sources:
        return {
            "answer": "I couldn't find anything you have access to about that.",
            "insufficient": True,
        }
    if ctx.provider is None:
        return {"answer": "", "insufficient": True}  # extractive mode: caller shows sources only
    listing = "\n\n".join(f"[{s.n}] ({s.meta}) {s.title or ''}\n{s.text}" for s in sources)
    from ..graph.costs import BudgetExceeded

    if ctx.budget_remaining_usd <= 0:
        raise BudgetExceeded("Workspace LLM budget used up for this month")
    data, usage = await ctx.provider.complete_structured(
        [
            ChatMessage("system", SYSTEM),
            ChatMessage("user", f"Question: {state['question']}\n\nSources:\n\n{listing}"),
        ],
        AnswerSchema,
        tier="default",
        max_tokens=2000,
    )
    ctx.usage.append(usage)
    return {"answer": data["answer"], "insufficient": bool(data["insufficient"])}


async def validate(state: AskState, ctx: AskContext) -> AskState:
    valid = {s.n for s in state.get("sources") or []}
    text_ = state.get("answer") or ""
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text_)} & valid)
    # Remove markers pointing at sources that weren't retrieved.
    cleaned = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if int(m.group(1)) in valid else "", text_)
    return {"answer": cleaned, "cited": cited}


async def _retrieve(state: AskState, runtime: Runtime[AskContext]) -> AskState:
    return await retrieve(state, runtime.context)


async def _answer(state: AskState, runtime: Runtime[AskContext]) -> AskState:
    return await answer(state, runtime.context)


async def _validate(state: AskState, runtime: Runtime[AskContext]) -> AskState:
    return await validate(state, runtime.context)


def build_graph():
    g = StateGraph(AskState, context_schema=AskContext)
    g.add_node("retrieve", _retrieve)
    g.add_node("answer", _answer)
    g.add_node("validate", _validate)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "answer")
    g.add_edge("answer", "validate")
    g.add_edge("validate", END)
    return g.compile()


_graph = None


async def ask(question: str, ctx: AskContext) -> AskState:
    global _graph
    if _graph is None:
        _graph = build_graph()
    return await _graph.ainvoke({"question": question}, context=ctx)


async def record_agent_usage(session_factory, workspace_id: str, usage: list[Usage]) -> None:
    if not usage:
        return
    async with session_factory(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO llm_usage (workspace_id, purpose, provider, model, input_tokens,"
                " cached_input_tokens, output_tokens, cost_usd) VALUES (:ws, 'agent', :p, :m,"
                " :i, :c, :o, :cost)"
            ),
            [
                {
                    "ws": workspace_id,
                    "p": u.provider,
                    "m": u.model,
                    "i": u.input_tokens,
                    "c": u.cached_input_tokens,
                    "o": u.output_tokens,
                    "cost": u.cost_usd,
                }
                for u in usage
            ],
        )
