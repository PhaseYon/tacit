"""The Larkspur Labs demo dataset: loads cleanly, idempotently and permission-safely."""

import json
from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy import text

from tacit_api import demo
from tacit_api.connectors.github import normalize as n
from tacit_api.db import dispose_engine, rls_session, worker_session
from tacit_api.search.hybrid import hybrid_search

from .conftest import MIGRATOR_URL, Seed

WS = "org_larkspur_demo"
READER = "u_demo_reader"  # member with access to the private demo repos
OUTSIDER = "u_demo_outsider"  # member without it


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


def _questions() -> list[dict]:
    path = demo.demo_dir() / "benchmark" / "questions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(scope="module")
async def loaded(seed: Seed) -> dict:
    now = datetime.now(UTC)
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            'INSERT INTO organization (id, name, slug, "createdAt") VALUES (%s, %s, %s, %s)'
            " ON CONFLICT DO NOTHING",
            (WS, "Larkspur Demo", "larkspur-demo", now),
        )
        for i, uid in enumerate([READER, OUTSIDER]):
            conn.execute(
                'INSERT INTO "user" (id, name, email, "emailVerified") VALUES (%s, %s, %s, true)'
                " ON CONFLICT DO NOTHING",
                (uid, uid, f"{uid}@example.com"),
            )
            conn.execute(
                'INSERT INTO member (id, "organizationId", "userId", role, "createdAt")'
                " VALUES (%s, %s, %s, 'member', %s) ON CONFLICT DO NOTHING",
                (f"m_demo_{i}", WS, uid, now),
            )
    summary = await demo.load_demo(WS, private_access_user_ids=[READER])
    await dispose_engine()
    return summary


async def _scalar(sql: str, **params):
    async with worker_session(WS) as s:
        return await s.scalar(text(sql), params)


async def _rows(sql: str, **params):
    async with worker_session(WS) as s:
        return (await s.execute(text(sql), params)).all()


def _expected_counts(data: dict) -> dict[str, int]:
    """What should survive normalization, computed straight from the JSON."""

    def human(key: str) -> list[dict]:
        return [x for x in data[key] if not n.is_bot(x.get("user"))]

    return {
        "issue": len(human("issues")),
        "pull_request": len(human("pull_requests")),
        "issue_comment": len(human("issue_comments")),
        "pr_review_comment": len(human("review_comments")),
        "pr_review": len(
            [r for r in human("pull_request_reviews") if (r.get("body") or "").strip()]
        ),
        "markdown_file": len(data["markdown_files"]),
    }


async def test_counts_match_json(loaded):
    for name, data in demo.load_repo_files().items():
        full = data["repository"]["full_name"]
        cid = loaded["repos"][full]["container_id"]
        rows = await _rows(
            "SELECT kind, count(*) FROM source_document"
            " WHERE container_id = :cid AND deleted_at IS NULL GROUP BY kind",
            cid=cid,
        )
        got = {kind: count for kind, count in rows}
        expected = {k: v for k, v in _expected_counts(data).items() if v}
        assert got == expected, name


async def test_dataset_size_is_as_advertised(loaded):
    files = demo.load_repo_files()
    prs = sum(len(d["pull_requests"]) for d in files.values())
    issues = sum(len(d["issues"]) for d in files.values())
    comments = sum(
        len(d["issue_comments"]) + len(d["review_comments"]) + len(d["pull_request_reviews"])
        for d in files.values()
    )
    docs = sum(len(d["markdown_files"]) for d in files.values())
    assert 60 <= prs <= 90
    assert 30 <= issues <= 50
    assert comments >= 150
    assert docs >= 12
    assert {d["repository"]["visibility"] for d in files.values()} == {"public", "private"}


async def test_loader_is_idempotent(loaded):
    snapshot_sql = (
        "SELECT external_id, url, content_hash, processed_hash, parent_id,"
        " metadata::text, source_updated_at FROM source_document"
        " WHERE workspace_id = :ws ORDER BY external_id"
    )
    before = await _rows(snapshot_sql, ws=WS)
    chunks_before = await _scalar(
        "SELECT count(*) FROM document_chunk WHERE workspace_id = :ws", ws=WS
    )

    again = await demo.load_demo(WS, private_access_user_ids=[READER])

    assert await _rows(snapshot_sql, ws=WS) == before
    assert again["indexed"]["documents"] == 0  # nothing re-embedded
    assert again["installation_id"] == loaded["installation_id"]
    assert {k: v["container_id"] for k, v in again["repos"].items()} == {
        k: v["container_id"] for k, v in loaded["repos"].items()
    }
    assert (
        await _scalar("SELECT count(*) FROM document_chunk WHERE workspace_id = :ws", ws=WS)
        == chunks_before
    )
    assert (
        await _scalar("SELECT count(*) FROM container_access WHERE workspace_id = :ws", ws=WS) == 2
    )


async def test_every_gold_source_is_a_loaded_document(loaded):
    rows = await _rows(
        "SELECT url FROM source_document WHERE workspace_id = :ws AND deleted_at IS NULL", ws=WS
    )
    urls = {r[0] for r in rows}
    questions = _questions()
    assert len(questions) >= 45
    missing = [(q["id"], u) for q in questions for u in q["gold_sources"] if u not in urls]
    assert missing == []


async def test_bot_content_is_filtered(loaded):
    assert (
        await _scalar(
            "SELECT count(*) FROM source_document WHERE workspace_id = :ws"
            " AND author_login LIKE '%[bot]'",
            ws=WS,
        )
        == 0
    )
    bot_urls = [
        x["html_url"]
        for d in demo.load_repo_files().values()
        for key in ("pull_requests", "issue_comments")
        for x in d[key]
        if n.is_bot(x["user"])
    ]
    assert bot_urls  # the dataset really contains bot noise
    assert (
        await _scalar(
            "SELECT count(*) FROM source_document WHERE workspace_id = :ws AND url = ANY(:u)",
            ws=WS,
            u=bot_urls,
        )
        == 0
    )


async def test_comments_and_reviews_are_linked_to_their_parent(loaded):
    orphans = await _scalar(
        "SELECT count(*) FROM source_document WHERE workspace_id = :ws"
        " AND kind IN ('issue_comment', 'pr_review', 'pr_review_comment') AND parent_id IS NULL",
        ws=WS,
    )
    assert orphans == 0


async def test_pr_approvals_recorded(loaded):
    approved = await _scalar(
        "SELECT metadata->'approved_by' FROM source_document WHERE workspace_id = :ws"
        " AND kind = 'pull_request' AND title LIKE 'ADR-0012:%'",
        ws=WS,
    )
    assert approved == ["maya-chen", "tomas-rivera"]


async def test_demo_installation_is_suspended(loaded):
    # Scheduled reconcile/ACL jobs must never try the real GitHub API with a fake id.
    async with worker_session() as s:
        listed = (await s.execute(text("SELECT * FROM app.list_github_installations()"))).all()
    assert loaded["installation_id"] not in {iid for _, iid in listed}


async def _search(user: str, q: str):
    async with rls_session(user, WS) as s:
        return await hybrid_search(s, q, limit=20)


async def test_private_repos_hidden_without_access(loaded):
    reader = await _search(READER, "Elasticsearch reindex outage postmortem")
    outsider = await _search(OUTSIDER, "Elasticsearch reindex outage postmortem")
    assert {h.container for h in reader} >= {"larkspur/api", "larkspur/handbook"}
    assert outsider and {h.container for h in outsider} == {"larkspur/handbook"}

    async with rls_session(OUTSIDER, WS) as s:
        visible = await s.scalar(
            text(
                "SELECT count(*) FROM source_document d JOIN source_container c"
                " ON c.id = d.container_id WHERE c.visibility = 'private'"
            )
        )
    assert visible == 0


async def test_search_pipeline_runs_for_every_question(loaded):
    # Retrieval quality is measured by the real benchmark (fake embedder here); this
    # only checks that every question goes through hybrid search without errors.
    for q in _questions():
        hits = await _search(READER, q["question"])
        assert isinstance(hits, list), q["id"]
