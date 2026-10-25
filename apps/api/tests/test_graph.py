"""Graph layer against a real Neo4j with a deterministic fake LLM (no network, no spend).

Covers: extraction pipeline, per-repo groups, fail-closed reads (no access, forged
workspace, empty groups never reach Graphiti, Postgres post-check), person linking,
budget hard stop, idempotency and edits.
"""

import asyncio
import socket
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text

from tacit_api.config import get_settings
from tacit_api.connectors.documents import NormalizedDocument
from tacit_api.connectors.github import ingest, sync
from tacit_api.db import dispose_engine, rls_session, worker_session
from tacit_api.llm.replay import ReplayProvider

from . import graph_fake_llm
from .conftest import MIGRATOR_URL, Seed
from .github_mock import repo_json


def _neo4j_up() -> bool:
    try:
        with socket.create_connection(("localhost", 7687), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _neo4j_up(), reason="Neo4j not running (compose profile graph)")

INST = 9700


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


def pr(ext, number, title, body, author, merged="2026-02-01T00:00:00+00:00"):
    return NormalizedDocument(
        kind="pull_request",
        external_id=ext,
        url=f"https://github.com/acme/x/pull/{number}",
        title=title,
        body=body,
        author_external_id=f"gh-{author}",
        author_login=author,
        source_created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_updated_at=datetime.fromisoformat(merged),
        metadata={"number": number, "state": "closed", "merged_at": merged},
    )


@pytest.fixture(scope="module")
def provider() -> ReplayProvider:
    return ReplayProvider(Path("/nonexistent"), responder=graph_fake_llm.respond)


@pytest.fixture(scope="module")
async def graph(seed: Seed, provider):
    from graphiti_core.utils.maintenance.graph_data_operations import clear_data

    from tacit_api.graph.client import build_graphiti, group_for

    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO github_installation (workspace_id, installation_id, account_id,"
            " account_login, account_type) VALUES (%s, %s, 1, 'acme', 'Organization')"
            " ON CONFLICT DO NOTHING",
            (seed.acme, INST),
        )
    public = await sync.upsert_repo(seed.acme, INST, repo_json(71, "handbook-g", private=False))
    private = await sync.upsert_repo(seed.acme, INST, repo_json(72, "core-g"))
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO container_access (container_id, user_id, workspace_id, permission)"
            " VALUES (%s, %s, %s, 'admin')",
            (private.container_id, seed.alice, seed.acme),
        )
        conn.execute(
            "UPDATE source_container SET acl_synced_at = now() WHERE id = %s",
            (private.container_id,),
        )
        conn.execute(
            "INSERT INTO workspace_graph_settings (workspace_id, enabled, include_private,"
            " monthly_budget_usd) VALUES (%s, true, true, 5) ON CONFLICT (workspace_id) DO UPDATE"
            " SET enabled = true, include_private = true, monthly_budget_usd = 5, paused_reason = NULL",
            (seed.acme,),
        )
    await ingest.upsert_documents(
        seed.acme,
        public.container_id,
        [
            pr(
                "G_PR_1",
                1,
                "Search on Postgres",
                "For search we switch from Elasticsearch to Postgres.",
                "maya",
            )
        ],
    )
    await ingest.upsert_documents(
        seed.acme,
        private.container_id,
        [
            pr(
                "G_PR_2",
                2,
                "Billing provider",
                "For billing we switch from Braintree to Stripe.",
                "omar",
            )
        ],
    )
    g = build_graphiti(provider)
    await g.build_indices_and_constraints()
    groups = [group_for(public.container_id), group_for(private.container_id)]
    yield {"g": g, "public": public.container_id, "private": private.container_id, "groups": groups}
    await clear_data(g.driver, group_ids=groups)
    await g.close()


async def run_pipeline(seed, graph, provider):
    from tacit_api.graph import pipeline

    counts = await pipeline.scan(seed.acme, provider)
    for cid in (graph["public"], graph["private"]):
        await pipeline.process_container(seed.acme, cid, graph["g"], provider)
    return counts


async def facts_for(user, ws, graph, queries=("search Postgres", "billing Stripe"), **kw):
    """Union of facts for a few focused queries (the fake embedder is weak on long ones)."""
    from tacit_api.graph.access import search_facts

    out, seen = [], set()
    for q in queries:
        async with rls_session(user, ws) as s:
            for f in await search_facts(s, graph["g"], q, **kw):
                if f.fact not in seen:
                    seen.add(f.fact)
                    out.append(f)
    return out


async def test_pipeline_extracts_and_cites(seed, graph, provider):
    counts = await run_pipeline(seed, graph, provider)
    assert counts["pending"] >= 2  # other modules may leave eligible docs in this workspace
    async with worker_session(seed.acme) as s:
        statuses = dict(
            (
                await s.execute(
                    text(
                        "SELECT root_document_id::text, status FROM graph_episode WHERE container_id IN (CAST(:a AS uuid), CAST(:b AS uuid))"
                    ),
                    {"a": graph["public"], "b": graph["private"]},
                )
            ).all()
        )
        usage = await s.scalar(
            text("SELECT count(*) FROM llm_usage WHERE purpose = 'graph_extract'")
        )
    assert set(statuses.values()) == {"done"}
    assert usage >= 2  # every LLM call is recorded

    facts = await facts_for(seed.alice, seed.acme, graph)
    by_fact = {f.fact: f for f in facts}
    assert "search uses Postgres instead of Elasticsearch" in by_fact
    assert "billing uses Stripe instead of Braintree" in by_fact
    assert by_fact["billing uses Stripe instead of Braintree"].citations[0].url.endswith("/pull/2")


async def test_private_facts_hidden_without_access(seed, graph, provider):
    facts = [f.fact for f in await facts_for(seed.bob, seed.acme, graph)]
    assert "search uses Postgres instead of Elasticsearch" in facts
    assert "billing uses Stripe instead of Braintree" not in facts


async def test_no_groups_means_graphiti_is_never_called(seed, graph, monkeypatch):
    calls = []
    original = graph["g"].search_

    async def spy(*a, **kw):
        calls.append(kw.get("group_ids"))
        return await original(*a, **kw)

    monkeypatch.setattr(graph["g"], "search_", spy)
    assert await facts_for(seed.carol, seed.acme, graph) == []  # forged workspace
    assert await facts_for(seed.alice, None, graph) == []  # no workspace selected
    assert calls == []


async def test_postgres_post_check_hides_facts_whose_episode_is_not_done(seed, graph):
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "UPDATE graph_episode SET status = 'stale' WHERE container_id = %s", (graph["private"],)
        )
    try:
        facts = [f.fact for f in await facts_for(seed.alice, seed.acme, graph)]
        # Still in Neo4j, but Postgres no longer vouches for it.
        assert "billing uses Stripe instead of Braintree" not in facts
    finally:
        with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
            conn.execute(
                "UPDATE graph_episode SET status = 'done' WHERE container_id = %s",
                (graph["private"],),
            )


async def test_people_linked_by_exact_login(seed, graph):
    async with worker_session(seed.acme) as s:
        linked = (
            await s.execute(
                text(
                    "SELECT lower(e.login) FROM graph_entity_link l JOIN external_identity e"
                    " ON e.id = l.external_identity_id WHERE l.container_id = CAST(:c AS uuid)"
                ),
                {"c": graph["public"]},
            )
        ).scalars()
        assert list(linked) == ["maya"]


async def test_rescan_is_idempotent(seed, graph, provider):
    from tacit_api.graph import pipeline

    assert (await pipeline.scan(seed.acme, provider))["pending"] == 0


async def test_edit_replaces_episode_and_old_facts_go(seed, graph, provider):
    from tacit_api.graph import pipeline

    await ingest.upsert_documents(
        seed.acme,
        graph["public"],
        [
            pr(
                "G_PR_1",
                1,
                "Search on Postgres",
                "For search we switch from Elasticsearch to Redis.",
                "maya",
                merged="2026-03-01T00:00:00+00:00",
            )
        ],
    )
    assert (await pipeline.scan(seed.acme, provider))["pending"] == 1
    await pipeline.process_container(seed.acme, graph["public"], graph["g"], provider)
    episodes = await graph["g"].driver.execute_query(
        "MATCH (e:Episodic) WHERE e.group_id = $g RETURN count(e) AS n", g=graph["groups"][0]
    )
    assert episodes.records[0]["n"] == 1
    facts = [
        f.fact
        for f in await facts_for(
            seed.bob, seed.acme, graph, queries=("search Redis", "search Postgres")
        )
    ]
    assert "search uses Redis instead of Elasticsearch" in facts
    assert "search uses Postgres instead of Elasticsearch" not in facts


async def test_budget_hard_stop_writes_nothing(seed, graph, provider):
    from tacit_api.graph import pipeline

    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "UPDATE workspace_graph_settings SET monthly_budget_usd = 0 WHERE workspace_id = %s",
            (seed.acme,),
        )
    try:
        await ingest.upsert_documents(
            seed.acme,
            graph["private"],
            [pr("G_PR_3", 3, "Queue", "For api we adopt Kafka.", "omar")],
        )
        await pipeline.scan(seed.acme, provider)
        stats = await pipeline.process_container(seed.acme, graph["private"], graph["g"], provider)
        assert stats["skipped_budget"] == 1 and stats["done"] == 0
        res = await graph["g"].driver.execute_query(
            "MATCH (e:Episodic) WHERE e.group_id = $g AND e.name CONTAINS 'Queue' RETURN count(e) AS n",
            g=graph["groups"][1],
        )
        assert res.records[0]["n"] == 0
    finally:
        with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
            conn.execute(
                "UPDATE workspace_graph_settings SET monthly_budget_usd = 5 WHERE workspace_id = %s",
                (seed.acme,),
            )


def test_llm_calls_outside_a_cost_context_are_refused(provider):
    from tacit_api.graph.adapters import TacitGraphitiLLMClient
    from tacit_api.graph.costs import BudgetExceeded

    client = TacitGraphitiLLMClient(provider)
    with pytest.raises(BudgetExceeded):
        asyncio.run(client._generate_response([], None))


def test_settings_default_graph_off():
    assert get_settings().graph_enabled is False
    assert Decimal("0") == Decimal(0)
