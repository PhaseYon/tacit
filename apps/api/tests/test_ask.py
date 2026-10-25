"""The ask workflow: grounded answers with validated citations, budget-aware, RLS-scoped."""

from decimal import Decimal
from pathlib import Path

import pytest

from tacit_api.agent.ask import AskContext, ask
from tacit_api.db import dispose_engine, rls_session
from tacit_api.llm.replay import ReplayProvider

from .test_search import corpus  # noqa: F401  (module fixture: public ADR + private PR)


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


def fake_llm(answer_text: str, insufficient: bool = False):
    seen = {}

    def responder(messages, schema):
        seen["prompt"] = messages[-1].content
        return {"answer": answer_text, "insufficient": insufficient}

    return ReplayProvider(Path("/nonexistent"), responder=responder), seen


async def run_ask(user, ws, question, provider, budget=Decimal(5)):
    async with rls_session(user, ws) as s:
        ctx = AskContext(s, provider, None, ws, budget)
        return await ask(question, ctx), ctx


async def test_answer_cites_only_retrieved_sources(seed, corpus):  # noqa: F811
    provider, seen = fake_llm("We use Postgres with pgvector [1]. Invented claim [99].")
    state, ctx = await run_ask(seed.alice, seed.acme, "why did we choose postgres", provider)
    assert "[1]" in state["answer"] and "[99]" not in state["answer"]
    assert state["cited"] == [1]
    assert state["sources"][0].title.startswith("ADR-0007")
    assert "ADR-0007" in seen["prompt"]
    assert len(ctx.usage) == 1


async def test_private_sources_never_reach_the_llm_for_users_without_access(seed, corpus):  # noqa: F811
    provider, seen = fake_llm("Answer [1].")
    await run_ask(seed.bob, seed.acme, "why did we switch billing to stripe", provider)
    assert (
        "Stripe" not in seen.get("prompt", "") or "Switch billing to Stripe" not in seen["prompt"]
    )


async def test_sources_only_without_llm(seed, corpus):  # noqa: F811
    state, ctx = await run_ask(seed.alice, seed.acme, "why postgres", None)
    assert state["answer"] == "" and state["sources"] and ctx.usage == []


async def test_budget_stop(seed, corpus):  # noqa: F811
    from tacit_api.graph.costs import BudgetExceeded

    provider, _ = fake_llm("x [1]")
    with pytest.raises(BudgetExceeded):
        await run_ask(seed.alice, seed.acme, "why postgres", provider, budget=Decimal(0))


async def test_ask_endpoint_without_llm_key(client, make_token, seed, corpus, monkeypatch):  # noqa: F811
    from tacit_api.config import get_settings
    from tacit_api.llm import get_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    get_provider.cache_clear()
    r = await client.post(
        f"/workspaces/{seed.acme}/ask",
        json={"question": "why did we pick postgres"},
        headers={"Authorization": f"Bearer {make_token(seed.alice)}"},
    )
    get_settings.cache_clear()
    get_provider.cache_clear()
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "sources_only" and body["sources"] and body["cost_usd"] == 0
