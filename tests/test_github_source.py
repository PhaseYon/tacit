"""GitHubSource against a mocked GitHub API (respx)."""

import json
from datetime import UTC, datetime

import pytest
import respx

from tacit.config import Config
from tacit.sources.base import ScopeEntry, SourceAccessDenied
from tacit.sources.github.source import GitHubSource
from tacit.store import Store
from tacit.store import docs as d
from tacit.store import state as st
from tacit.sync import run_sync

from .github_mock import FakeGitHub, repo_json

ENTRY = ScopeEntry("github:acme/api", "github", "acme/api", "acme/api", {"private": True})


def _gh() -> FakeGitHub:
    gh = FakeGitHub(repo_json(42, "api"))
    # Merged PR with a review discussion and an approval.
    gh.add_issue(
        10,
        "Use Redis for the API cache",
        "dana",
        updated="2026-03-04T00:00:00Z",
        body="Postgres caching is too slow under load.",
        state="closed",
        closed_at="2026-03-04T00:00:00Z",
        pr=True,
        merged_at="2026-03-04T00:00:00Z",
        labels=("perf",),
    )
    gh.add_comment(10, "lee", "Did we consider memcached?")
    gh.add_comment(10, "dependabot[bot]", "Bumps redis from 4 to 5.")
    gh.add_review(10, "lee", "CHANGES_REQUESTED", "Needs a TTL.")
    gh.add_review(10, "lee", "APPROVED")
    gh.add_inline(10, "sam", "api/cache.py", "Set the TTL from config.")
    # Open PR (indexed, not extracted).
    gh.add_issue(11, "WIP: Kafka for billing", "lee", updated="2026-03-05T00:00:00Z", pr=True)
    # Bot PR (skipped).
    gh.add_issue(12, "Bump redis", "dependabot[bot]", updated="2026-03-06T00:00:00Z", pr=True)
    # Closed issue with two human comments (extracted) and one with a single comment.
    gh.add_issue(
        20,
        "Cache stampede on deploy",
        "sam",
        updated="2026-03-07T00:00:00Z",
        state="closed",
        closed_at="2026-03-07T00:00:00Z",
    )
    gh.add_comment(20, "dana", "Add jitter to TTLs.")
    gh.add_comment(20, "sam", "Done in #10.")
    gh.add_issue(
        21,
        "Typo in README",
        "kim",
        updated="2026-03-08T00:00:00Z",
        state="closed",
        closed_at="2026-03-08T00:00:00Z",
    )
    gh.add_comment(21, "dana", "Fixed.")
    gh.files = {
        "README.md": "# Acme API\n\nThe API.",
        "docs/adr/0001-redis.md": "# ADR 1: Redis\n\nWe chose Redis.",
        "src/main.py": "print('not a doc')",
    }
    return gh


async def _pages(src: GitHubSource, cursor: str | None, entry: ScopeEntry = ENTRY):
    return [p async for p in src.fetch(entry, cursor)]


def _items(pages):
    return {i.external_id: i for p in pages for i in p.items}


@pytest.fixture
def gh():
    fake = _gh()
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        yield fake


@pytest.fixture
def src() -> GitHubSource:
    return GitHubSource("ghp_test", Config())


async def test_resolve(gh, src):
    entry = await src.resolve("https://github.com/acme/api")
    assert entry == ScopeEntry(
        "github:acme/api", "github", "acme/api", "acme/api", {"private": True}
    )
    with pytest.raises(ValueError, match="not found"):
        await src.resolve("acme/missing")
    with pytest.raises(ValueError, match="owner/repo"):
        await src.resolve("acme")


async def test_first_sync_renders_items_and_flags(gh, src):
    pages = await _pages(src, None)
    items = _items(pages)
    assert set(items) == {
        "github:PR_10",
        "github:PR_11",
        "github:I_20",
        "github:I_21",
        "github:42:README.md",
        "github:42:docs/adr/0001-redis.md",
    }  # bot PR 12 and src/main.py skipped

    pr = items["github:PR_10"]
    assert pr.kind == "pull_request" and pr.url == "https://github.com/acme/api/pull/10"
    assert "State: merged" in pr.text and "Labels: perf" in pr.text
    assert "Approved by: @lee" in pr.text
    assert "@dana (PR author, 2026-03-01): Postgres caching" in pr.text
    assert "@lee (review, changes_requested, 2026-03-03): Needs a TTL." in pr.text
    assert "@sam (comment on api/cache.py" in pr.text
    assert "dependabot" not in pr.text
    assert pr.participants == ("github:dana", "github:lee", "github:sam")
    assert pr.reference_time == datetime(2026, 3, 4, tzinfo=UTC)
    assert pr.extract is True

    assert items["github:PR_11"].extract is False  # open PR
    assert "State: open" in items["github:PR_11"].text
    assert items["github:I_20"].extract is True  # closed, 2 human comments
    assert items["github:I_21"].extract is False  # closed, 1 comment
    readme = items["github:42:README.md"]
    assert readme.kind == "markdown_file" and readme.extract is True
    assert readme.title == "Acme API"
    assert readme.url == "https://github.com/acme/api/blob/main/README.md"

    # One page per issues API page (2 per page in the mock), then the docs page.
    assert len(pages) == 4
    cursor = json.loads(pages[-1].cursor)
    assert cursor["since"] == "2026-03-08T00:00:00Z" and cursor["seen"] == ["I_21"]
    assert cursor["docs_tree"] == gh.tree_sha
    assert set(cursor["doc_blobs"]) == {"README.md", "docs/adr/0001-redis.md"}


async def test_incremental_sync_only_fetches_what_changed(gh, src):
    first = await _pages(src, None)
    cursor = first[-1].cursor
    gh.calls.clear()

    nothing = await _pages(src, cursor)
    assert _items(nothing) == {}
    # Repo lookup, one issues page (the boundary item, skipped), branch lookup. No
    # comments, reviews, tree or blob calls.
    assert len(gh.calls) == 3 and gh.count("/branches/") == 1
    assert gh.count("/git/") == 0 and gh.count("comments|reviews") == 0

    # A new comment bumps issue 20; only it is re-fetched.
    gh.add_comment(20, "lee", "Reopening if it happens again.")
    gh.issues[20]["updated_at"] = "2026-03-09T00:00:00Z"
    gh.calls.clear()
    pages = await _pages(src, nothing[-1].cursor or cursor)
    items = _items(pages)
    assert list(items) == ["github:I_20"]
    assert "Reopening if it happens again." in items["github:I_20"].text
    # 3 comments at 2 per page = 2 calls; nothing for the unchanged items.
    assert gh.count(r"/issues/\d+/comments") == 2 and gh.count("/issues/20/") == 2
    assert gh.count("/pulls/") == 0
    assert json.loads(pages[-1].cursor)["since"] == "2026-03-09T00:00:00Z"


async def test_docs_pass_skips_unchanged_tree_and_blobs(gh, src):
    cursor = (await _pages(src, None))[-1].cursor
    gh.files["docs/adr/0001-redis.md"] += "\n\nUpdate: TTLs are 5 minutes."
    gh.calls.clear()
    pages = await _pages(src, cursor)
    items = _items(pages)
    assert list(items) == ["github:42:docs/adr/0001-redis.md"]
    assert "TTLs are 5 minutes" in items["github:42:docs/adr/0001-redis.md"].text
    assert gh.count("/git/blobs/") == 1 and gh.count("/git/trees/") == 1
    assert gh.count("/commits") == 1  # one date lookup per changed doc only
    assert json.loads(pages[-1].cursor)["docs_tree"] == gh.tree_sha


async def test_doc_date_is_the_files_last_commit_not_the_branch(gh, src):
    gh.file_dates["docs/adr/0001-redis.md"] = "2025-04-22T10:00:00Z"
    items = _items(await _pages(src, None))
    assert items["github:42:docs/adr/0001-redis.md"].reference_time == datetime(
        2025, 4, 22, 10, tzinfo=UTC
    )


async def test_fetch_without_access_raises(gh, src):
    gh.denied = True
    with pytest.raises(SourceAccessDenied):
        await _pages(src, None)


async def test_run_sync_end_to_end(gh, src, store: Store):
    await st.scope_add(store, ENTRY)
    r1 = await run_sync(store, {"github": src})
    assert r1.scopes[0].error is None
    assert r1.scopes[0].written == 6
    row = await store._execute(
        "MATCH (d:TacitDoc {external_id: 'github:PR_10'}) RETURN d.url AS url, d.extract AS x,"
        " d.participants AS p"
    )
    assert row[0]["url"].endswith("/pull/10") and row[0]["x"] is True
    assert "github:lee" in row[0]["p"]
    assert r1.extraction_pending == 4  # PR 10, issue 20, two docs

    r2 = await run_sync(store, {"github": src})
    assert r2.scopes[0].written == 0
    assert await d.known_hashes(store, ["github:I_21"])
