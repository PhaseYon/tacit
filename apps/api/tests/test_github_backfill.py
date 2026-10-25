"""Backfill, markdown sync and webhook handlers against a mocked GitHub API."""

import base64
import copy
import json
from pathlib import Path

import httpx
import psycopg
import pytest
import respx
from sqlalchemy import text

from tacit_api.connectors.github import backfill, client, handlers, sync
from tacit_api.db import dispose_engine, worker_session

from .conftest import MIGRATOR_URL, Seed
from .github_mock import API, mock_installation_token, write_app_key

FIX = Path(__file__).parent / "fixtures" / "github"
INST = 9300
REPO = "/repos/Codertocat/Hello-World"


def load(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def github_settings(monkeypatch, tmp_path):
    from tacit_api.config import get_settings

    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(write_app_key(tmp_path)))
    monkeypatch.setenv("GITHUB_CACHE", "memory")
    get_settings.cache_clear()
    client._private_key.cache_clear()
    yield
    get_settings.cache_clear()
    client._private_key.cache_clear()


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


@pytest.fixture
def queued(monkeypatch) -> list[tuple]:
    """Capture tasks that handlers enqueue instead of sending them to Redis."""
    calls: list[tuple] = []

    class FakeTask:
        def __init__(self, name: str):
            self.name = name

        def delay(self, *args):
            calls.append((self.name, *args))

    class FakeTasks:
        def __getattr__(self, name: str):
            return FakeTask(name)

    monkeypatch.setattr(handlers, "_tasks", lambda: FakeTasks())
    return calls


@pytest.fixture(scope="module")
async def repo(seed: Seed) -> backfill.Repo:
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO github_installation (workspace_id, installation_id, account_id,"
            " account_login, account_type) VALUES (%s, %s, 1, 'Codertocat', 'User')"
            " ON CONFLICT DO NOTHING",
            (seed.acme, INST),
        )
    ref = await sync.upsert_repo(seed.acme, INST, load("repository"))
    loaded = await backfill.load_repo(seed.acme, ref.container_id)
    await dispose_engine()
    assert loaded is not None
    return loaded


def mock_repo_api(router: respx.MockRouter) -> dict[str, respx.Route]:
    mock_installation_token(router, INST)
    issue, pr = load("issue"), load("pr_from_issues_listing")

    def issues_page(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[pr])
        nxt = f'<{API}{REPO}/issues?state=all&page=2>; rel="next"'
        return httpx.Response(200, json=[issue], headers={"Link": nxt})

    review = load("pr_review") | {"state": "APPROVED", "body": "LGTM, ship it."}
    routes = {
        "issues": router.get(f"{API}{REPO}/issues").mock(side_effect=issues_page),
        "reviews": router.get(f"{API}{REPO}/pulls/2/reviews").mock(
            return_value=httpx.Response(200, json=[review])
        ),
        "issue_comments": router.get(f"{API}{REPO}/issues/comments").mock(
            return_value=httpx.Response(200, json=[load("issue_comment")])
        ),
        "review_comments": router.get(f"{API}{REPO}/pulls/comments").mock(
            return_value=httpx.Response(200, json=[load("pr_review_comment")])
        ),
        "branch": router.get(f"{API}{REPO}/branches/master").mock(
            return_value=httpx.Response(
                200, json={"commit": {"sha": "c0ffee", "commit": {"tree": {"sha": "t1"}}}}
            )
        ),
        "tree": router.get(f"{API}{REPO}/git/trees/t1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {"path": "README.md", "type": "blob", "sha": "b-readme"},
                        {"path": "docs/adr/0001-postgres.md", "type": "blob", "sha": "b-adr"},
                        {"path": "src/main.py", "type": "blob", "sha": "b-code"},
                        {"path": "docs", "type": "tree", "sha": "t-docs"},
                    ],
                },
            )
        ),
    }
    blobs = {
        "b-readme": "# Hello World\n\nA demo repo.",
        "b-adr": "# ADR 1: Use Postgres\n\nWe chose Postgres because pgvector.",
    }
    routes["blobs"] = router.get(url__regex=rf"{API}{REPO}/git/blobs/.+").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "content": base64.b64encode(blobs[req.url.path.rsplit("/", 1)[1]].encode()).decode()
            },
        )
    )
    return routes


async def run_full_backfill(seed: Seed, repo: backfill.Repo) -> None:
    gh = client.installation_client(INST)
    for resource in backfill.ORDER:
        while await backfill.backfill_page(seed.acme, repo, resource, gh):
            pass


async def docs(seed: Seed, repo: backfill.Repo) -> dict[str, dict]:
    async with worker_session(seed.acme) as s:
        rows = await s.execute(
            text(
                "SELECT d.external_id, d.kind, d.title, d.url, d.metadata, d.deleted_at,"
                " encode(d.content_hash, 'hex') AS hash, p.external_id AS parent"
                " FROM source_document d LEFT JOIN source_document p ON p.id = d.parent_id"
                " WHERE d.container_id = :cid"
            ),
            {"cid": repo.container_id},
        )
        return {r.external_id: r._asdict() for r in rows}


@respx.mock
async def test_backfill_ingests_everything_with_citations(seed, repo):
    routes = mock_repo_api(respx.mock)
    await run_full_backfill(seed, repo)
    got = await docs(seed, repo)

    kinds = sorted(d["kind"] for d in got.values())
    assert kinds == [
        "issue",
        "issue_comment",
        "markdown_file",
        "markdown_file",
        "pr_review",
        "pr_review_comment",
        "pull_request",
    ]
    issue_id, pr_id = load("issue")["node_id"], load("pr_from_issues_listing")["node_id"]
    assert got[load("issue_comment")["node_id"]]["parent"] == issue_id
    assert got[load("pr_review_comment")["node_id"]]["parent"] == pr_id
    assert got[load("pr_review")["node_id"]]["parent"] == pr_id
    assert got[pr_id]["metadata"]["approved_by"] == ["Codertocat"]
    adr = got[f"{repo.repo_id}:docs/adr/0001-postgres.md"]
    assert adr["title"] == "ADR 1: Use Postgres"
    assert (
        adr["url"]
        == "https://github.com/Codertocat/Hello-World/blob/c0ffee/docs/adr/0001-postgres.md"
    )
    assert routes["issues"].call_count == 2  # followed the Link header

    async with worker_session(seed.acme) as s:
        cursors = dict(
            (
                await s.execute(
                    text(
                        "SELECT resource, next_url IS NULL AND since IS NOT NULL"
                        " FROM github_sync_cursor WHERE container_id = :c"
                    ),
                    {"c": repo.container_id},
                )
            ).all()
        )
    assert cursors == {"issues": True, "issue_comments": True, "review_comments": True}


@respx.mock
async def test_rerun_is_idempotent_and_skips_unchanged_blobs(seed, repo):
    routes = mock_repo_api(respx.mock)
    await run_full_backfill(seed, repo)
    before = await docs(seed, repo)
    blob_calls = routes["blobs"].call_count
    await run_full_backfill(seed, repo)
    after = await docs(seed, repo)
    assert {k: v["hash"] for k, v in after.items()} == {k: v["hash"] for k, v in before.items()}
    assert routes["blobs"].call_count == blob_calls  # same blob SHAs -> no downloads
    # Re-runs list from the saved cursor (minus the overlap), not from the beginning.
    assert "since" in routes["issue_comments"].calls.last.request.url.params


@respx.mock
async def test_deleted_markdown_file_is_tombstoned(seed, repo):
    routes = mock_repo_api(respx.mock)
    await run_full_backfill(seed, repo)
    routes["tree"].mock(
        return_value=httpx.Response(
            200, json={"tree": [{"path": "README.md", "type": "blob", "sha": "b-readme"}]}
        )
    )
    await backfill.sync_markdown(seed.acme, repo, client.installation_client(INST))
    got = await docs(seed, repo)
    assert got[f"{repo.repo_id}:docs/adr/0001-postgres.md"]["deleted_at"] is not None
    assert got[f"{repo.repo_id}:README.md"]["deleted_at"] is None


# --- webhook handlers -----------------------------------------------------------------


def _payload(**kw) -> dict:
    return {"installation": {"id": INST}, "repository": load("repository"), **kw}


@respx.mock
async def test_issue_webhook_updates_and_ignores_stale_events(seed, repo, queued):
    mock_repo_api(respx.mock)
    await run_full_backfill(seed, repo)
    issue = load("issue")

    newer = copy.deepcopy(issue) | {"title": "Renamed", "updated_at": "2030-01-01T00:00:00Z"}
    assert (
        await handlers.handle_issue_or_pr(seed.acme, _payload(action="edited", issue=newer))
        == "done"
    )
    assert (await docs(seed, repo))[issue["node_id"]]["title"] == "Renamed"

    # An older event delivered late must not roll the title back.
    older = copy.deepcopy(issue) | {"title": "Stale", "updated_at": "2020-01-01T00:00:00Z"}
    await handlers.handle_issue_or_pr(seed.acme, _payload(action="edited", issue=older))
    assert (await docs(seed, repo))[issue["node_id"]]["title"] == "Renamed"


@respx.mock
async def test_deleted_comment_webhook_tombstones(seed, repo, queued):
    mock_repo_api(respx.mock)
    await run_full_backfill(seed, repo)
    comment = load("issue_comment")
    await handlers.handle_issue_comment(seed.acme, _payload(action="deleted", comment=comment))
    assert (await docs(seed, repo))[comment["node_id"]]["deleted_at"] is not None


async def test_privatized_repo_narrows_access_immediately(seed, repo, queued):
    async with worker_session(seed.acme) as s:
        await s.execute(
            text("UPDATE source_container SET visibility = 'public' WHERE id = :c"),
            {"c": repo.container_id},
        )
    await handlers.handle_repository(seed.acme, _payload(action="privatized"))
    async with worker_session(seed.acme) as s:
        vis = await s.scalar(
            text("SELECT visibility FROM source_container WHERE id = :c"), {"c": repo.container_id}
        )
    assert vis == "private"
    assert queued == [("sync_installation", seed.acme, INST)]


async def test_push_to_other_branch_or_without_markdown_is_ignored(seed, repo, queued):
    base = _payload(ref="refs/heads/master", commits=[{"modified": ["src/main.py"]}])
    assert await handlers.handle_push(seed.acme, base) == "ignored"
    feature = _payload(ref="refs/heads/feature", commits=[{"modified": ["docs/a.md"]}])
    assert await handlers.handle_push(seed.acme, feature) == "ignored"
    docs_push = _payload(ref="refs/heads/master", commits=[{"added": ["docs/new.md"]}])
    assert await handlers.handle_push(seed.acme, docs_push) == "done"
    assert queued == [("sync_markdown", seed.acme, repo.container_id)]
