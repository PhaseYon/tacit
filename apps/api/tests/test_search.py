"""Indexing + hybrid search: idempotency, permission filtering, citations."""

from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy import text

from tacit_api.connectors.documents import NormalizedDocument
from tacit_api.connectors.github import ingest, sync
from tacit_api.db import dispose_engine, rls_session, worker_session
from tacit_api.search.hybrid import hybrid_search
from tacit_api.search.indexer import index_workspace

from .conftest import MIGRATOR_URL, Seed
from .github_mock import repo_json

INST = 9500


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


def doc(ext: str, kind: str, title: str | None, body: str, number: int | None = None, **meta):
    return NormalizedDocument(
        kind=kind,
        external_id=ext,
        url=f"https://github.com/acme/x/{ext}",
        title=title,
        body=body,
        author_external_id="1",
        author_login="alice",
        source_created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata={"number": number, **meta} if number else meta,
    )


@pytest.fixture(scope="module")
async def corpus(seed: Seed) -> dict[str, str]:
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO github_installation (workspace_id, installation_id, account_id,"
            " account_login, account_type) VALUES (%s, %s, 1, 'acme', 'Organization')"
            " ON CONFLICT DO NOTHING",
            (seed.acme, INST),
        )
    public = await sync.upsert_repo(seed.acme, INST, repo_json(51, "handbook", private=False))
    private = await sync.upsert_repo(seed.acme, INST, repo_json(52, "core"))
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO container_access (container_id, user_id, workspace_id, permission)"
            " VALUES (%s, %s, %s, 'write') ON CONFLICT DO NOTHING",
            (private.container_id, seed.alice, seed.acme),
        )
        conn.execute(
            "UPDATE source_container SET acl_synced_at = now() WHERE id = %s",
            (private.container_id,),
        )

    await ingest.upsert_documents(
        seed.acme,
        public.container_id,
        [
            doc(
                "adr1",
                "markdown_file",
                "ADR-0007: Use Postgres for search",
                "## Decision\n\nWe use Postgres with pgvector instead of a separate vector "
                "database, because one database is simpler to operate and back up.",
                path="docs/adr/0007.md",
            ),
            doc("i1", "issue", "Dark mode for settings page", "Users want a dark theme.", 1),
        ],
    )
    await ingest.upsert_documents(
        seed.acme,
        private.container_id,
        [
            doc(
                "pr9",
                "pull_request",
                "Switch billing to Stripe",
                "Moving from Braintree to Stripe because of lower fees and better webhooks.",
                9,
            ),
        ],
    )
    # Fixture may be set up again by modules that import it; indexing is idempotent.
    await index_workspace(seed.acme)
    await dispose_engine()
    return {"public": public.container_id, "private": private.container_id}


async def _search(user: str, ws: str, q: str):
    async with rls_session(user, ws) as s:
        return await hybrid_search(s, q)


async def test_finds_relevant_doc_with_citation(seed, corpus):
    hits = await _search(seed.alice, seed.acme, "why did we pick postgres pgvector")
    assert hits[0].title == "ADR-0007: Use Postgres for search"
    assert hits[0].url == "https://github.com/acme/x/adr1"
    assert hits[0].container == "acme/handbook"
    assert set(hits[0].matched_by) == {"semantic", "keyword"}
    assert "**" in hits[0].snippet  # highlighted terms


async def test_keyword_exact_identifier(seed, corpus):
    hits = await _search(seed.alice, seed.acme, "ADR-0007")
    assert hits and hits[0].title.startswith("ADR-0007")


async def test_private_repo_hidden_from_users_without_access(seed, corpus):
    alice = [h.title for h in await _search(seed.alice, seed.acme, "stripe billing fees")]
    bob = [h.title for h in await _search(seed.bob, seed.acme, "stripe billing fees")]
    assert "Switch billing to Stripe" in alice
    assert "Switch billing to Stripe" not in bob


async def test_other_workspace_sees_nothing(seed, corpus):
    assert await _search(seed.carol, seed.globex, "postgres") == []
    assert await _search(seed.carol, seed.acme, "postgres") == []  # forged workspace


async def test_reindex_skips_unchanged_and_picks_up_edits(seed, corpus):
    assert (await index_workspace(seed.acme)).documents == 0
    # Metadata-only change (e.g. approvals) must not trigger re-embedding.
    await ingest.set_pr_approvals(seed.acme, corpus["private"], 9, ["bob"])
    assert (await index_workspace(seed.acme)).documents == 0
    # Real content change does.
    await ingest.upsert_documents(
        seed.acme,
        corpus["public"],
        [doc("i1", "issue", "Dark mode for settings page", "Also needs high contrast.", 1)],
    )
    assert (await index_workspace(seed.acme)).documents == 1


async def test_tombstoned_docs_leave_the_index(seed, corpus):
    await ingest.upsert_documents(
        seed.acme, corpus["public"], [doc("tmp", "issue", "Kafka migration", "Move to Kafka.", 2)]
    )
    await index_workspace(seed.acme)
    assert [h.title for h in await _search(seed.alice, seed.acme, "kafka")][:1] == [
        "Kafka migration"
    ]
    await ingest.tombstone(seed.acme, ["tmp"])
    await index_workspace(seed.acme)
    assert "Kafka migration" not in [h.title for h in await _search(seed.alice, seed.acme, "kafka")]
    async with worker_session(seed.acme) as s:
        left = await s.scalar(
            text(
                "SELECT count(*) FROM document_chunk c JOIN source_document d"
                " ON d.id = c.document_id WHERE d.external_id = 'tmp'"
            )
        )
    assert left == 0


async def test_search_endpoint(client, make_token, seed, corpus):
    r = await client.get(
        f"/workspaces/{seed.acme}/search",
        params={"q": "postgres"},
        headers={"Authorization": f"Bearer {make_token(seed.alice)}"},
    )
    assert r.status_code == 200
    first = r.json()["results"][0]
    assert first["url"].startswith("https://github.com/")
    assert first["kind"] == "markdown_file"


async def test_baselines_respect_permissions(seed, corpus):
    from tacit_api.search.hybrid import baseline_search

    for mode in ("vector", "keyword"):
        async with rls_session(seed.alice, seed.acme) as s:
            alice = [h.title for h in await baseline_search(s, "stripe billing fees", mode)]
        async with rls_session(seed.bob, seed.acme) as s:
            bob = [h.title for h in await baseline_search(s, "stripe billing fees", mode)]
        assert "Switch billing to Stripe" in alice, mode
        assert "Switch billing to Stripe" not in bob, mode
