"""Unit tests for the GitHub normalizers. Pure: no DB, no network."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tacit.sources.documents import MAX_BODY_BYTES
from tacit.sources.github.normalize import (
    DEFAULT_DOC_GLOBS,
    approvals,
    is_bot,
    matches_doc_globs,
    normalize_issue,
    normalize_issue_comment,
    normalize_markdown_file,
    normalize_pr_review,
    normalize_pr_review_comment,
)

FIXTURES = Path(__file__).parent / "fixtures" / "github"

DEPENDABOT = {
    "login": "dependabot[bot]",
    "id": 49699333,
    "node_id": "MDM6Qm90NDk2OTkzMzM=",
    "html_url": "https://github.com/apps/dependabot",
    "type": "Bot",
    "site_admin": False,
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def repo() -> dict:
    return load("repository")


# --- issues and PRs --------------------------------------------------------


def test_issue(repo):
    doc = normalize_issue(load("issue"), repo)
    assert doc is not None
    assert doc.kind == "issue"
    assert doc.external_id == "MDU6SXNzdWU0NDQ1MDAwNDE="
    assert doc.url == "https://github.com/Codertocat/Hello-World/issues/1"
    assert doc.title == "Spelling error in the README file"
    assert doc.body == "It looks like you accidently spelled 'commit' with two 't's."
    assert doc.author_external_id == "21031067"
    assert doc.author_login == "Codertocat"
    assert doc.source_created_at == datetime(2019, 5, 15, 15, 20, 18, tzinfo=UTC)
    assert doc.source_updated_at is not None and doc.source_updated_at.tzinfo is not None
    assert doc.parent_external_id is None
    assert doc.metadata == {
        "number": 1,
        "state": "open",
        "state_reason": None,
        "labels": ["bug"],
        "merged_at": None,
        "draft": False,
        "assignees": ["Codertocat"],
    }
    assert doc.hash_fields == {
        "state": "open",
        "state_reason": None,
        "labels": ["bug"],
        "merged": False,
    }


def test_pr_from_issues_listing_is_pull_request_and_merged(repo):
    doc = normalize_issue(load("pr_from_issues_listing"), repo)
    assert doc is not None
    assert doc.kind == "pull_request"
    assert doc.url == "https://github.com/Codertocat/Hello-World/pull/2"
    assert doc.metadata["number"] == 2
    assert doc.metadata["merged_at"] == "2019-05-15T15:21:18Z"
    assert doc.hash_fields["merged"] is True


def test_pr_from_issues_listing_closed_unmerged(repo):
    item = load("pr_from_issues_listing")
    item["pull_request"]["merged_at"] = None
    doc = normalize_issue(item, repo)
    assert doc.kind == "pull_request"
    assert doc.hash_fields["merged"] is False
    assert doc.metadata["merged_at"] is None


def test_full_pr_object(repo):
    doc = normalize_issue(load("pull_request"), repo)
    assert doc is not None
    assert doc.kind == "pull_request"
    assert doc.external_id == "MDExOlB1bGxSZXF1ZXN0Mjc5MTQ3NDM3"
    assert doc.url == "https://github.com/Codertocat/Hello-World/pull/2"
    assert doc.metadata["state"] == "closed"
    assert doc.metadata["merged_at"] is None  # the example was closed without merging
    assert doc.metadata["base_ref"] == "master"
    assert doc.metadata["head_ref"] == "changes"
    assert doc.hash_fields["merged"] is False


def test_full_pr_merged(repo):
    pr = load("pull_request")
    pr["merged_at"] = "2019-05-15T15:21:18Z"
    pr["merged"] = True
    doc = normalize_issue(pr, repo)
    assert doc.hash_fields["merged"] is True
    assert doc.metadata["merged_at"] == "2019-05-15T15:21:18Z"


def test_draft_and_sorted_labels_and_assignees(repo):
    pr = load("pull_request")
    pr["draft"] = True
    pr["labels"] = [{"name": "zeta"}, {"name": "alpha"}, {"name": "mid"}]
    pr["assignees"] = [{"login": "zed", "id": 2}, {"login": "amy", "id": 1}]
    doc = normalize_issue(pr, repo)
    assert doc.metadata["draft"] is True
    assert doc.metadata["labels"] == ["alpha", "mid", "zeta"]
    assert doc.hash_fields["labels"] == ["alpha", "mid", "zeta"]
    assert doc.metadata["assignees"] == ["amy", "zed"]


def test_issue_state_reason(repo):
    issue = load("issue")
    issue["state"] = "closed"
    issue["state_reason"] = "not_planned"
    doc = normalize_issue(issue, repo)
    assert doc.metadata["state_reason"] == "not_planned"
    assert doc.hash_fields["state_reason"] == "not_planned"


def test_bot_authored_pr_is_skipped(repo):
    pr = load("pull_request")
    pr["user"] = DEPENDABOT
    assert normalize_issue(pr, repo) is None
    listing = load("pr_from_issues_listing")
    listing["user"] = DEPENDABOT
    assert normalize_issue(listing, repo) is None


def test_ghost_user_keeps_document(repo):
    issue = load("issue")
    issue["user"] = None
    doc = normalize_issue(issue, repo)
    assert doc is not None
    assert doc.author_external_id is None
    assert doc.author_login is None


def test_null_body(repo):
    issue = load("issue")
    issue["body"] = None
    doc = normalize_issue(issue, repo)
    assert doc.body == ""
    assert "truncated" not in doc.metadata


def test_crlf_and_trailing_whitespace_normalized(repo):
    issue = load("issue")
    issue["body"] = "line one   \r\nline two\t\r\n\r\n"
    doc = normalize_issue(issue, repo)
    assert doc.body == "line one\nline two"
    issue["body"] = "line one\nline two"
    assert normalize_issue(issue, repo).content_hash == doc.content_hash


def test_truncation_flag(repo):
    issue = load("issue")
    issue["body"] = "x" * (MAX_BODY_BYTES + 10)
    doc = normalize_issue(issue, repo)
    assert doc.metadata["truncated"] is True
    assert len(doc.body.encode()) == MAX_BODY_BYTES
    assert "truncated" not in normalize_issue(load("issue"), repo).metadata


# --- content hash stability ------------------------------------------------


def _hash(issue: dict, repo: dict) -> bytes:
    return normalize_issue(issue, repo).content_hash


def test_hash_ignores_volatile_fields(repo):
    base = load("issue")
    original = _hash(base, repo)
    noisy = copy.deepcopy(base)
    noisy["reactions"]["total_count"] = 42
    noisy["reactions"]["+1"] = 42
    noisy["comments"] = 17
    noisy["updated_at"] = "2024-01-01T00:00:00Z"
    noisy["labels"] = list(reversed(noisy["labels"]))
    assert _hash(noisy, repo) == original


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda i: i.update(body=i["body"] + " Edited."), id="body"),
        pytest.param(lambda i: i.update(title="New title"), id="title"),
        pytest.param(lambda i: i.update(state="closed"), id="state"),
        pytest.param(lambda i: i.update(state_reason="completed"), id="state_reason"),
        pytest.param(lambda i: i["labels"].append({"name": "wontfix"}), id="label_added"),
    ],
)
def test_hash_changes_on_content_change(repo, mutate):
    base = load("issue")
    changed = copy.deepcopy(base)
    mutate(changed)
    assert _hash(changed, repo) != _hash(base, repo)


def test_hash_changes_when_pr_merges(repo):
    item = load("pr_from_issues_listing")
    merged = _hash(item, repo)
    item["pull_request"]["merged_at"] = None
    assert _hash(item, repo) != merged


# --- issue comments --------------------------------------------------------


def test_issue_comment(repo):
    doc = normalize_issue_comment(load("issue_comment"), repo)
    assert doc is not None
    assert doc.kind == "issue_comment"
    assert doc.external_id == "MDEyOklzc3VlQ29tbWVudDQ5MjcwMDQwMA=="
    assert doc.url == "https://github.com/Codertocat/Hello-World/issues/1#issuecomment-492700400"
    assert doc.title is None
    assert doc.body == "You are totally right! I'll get this fixed right away."
    assert doc.parent_external_id is None
    assert doc.metadata["parent_number"] == 1
    assert isinstance(doc.metadata["parent_number"], int)
    assert doc.author_login == "Codertocat"
    assert doc.source_created_at == datetime(2019, 5, 15, 15, 20, 21, tzinfo=UTC)


def test_issue_comment_bot_and_ghost(repo):
    c = load("issue_comment")
    c["user"] = {"login": "codecov[bot]", "id": 1, "type": "User"}  # login suffix suffices
    assert normalize_issue_comment(c, repo) is None
    c["user"] = None
    doc = normalize_issue_comment(c, repo)
    assert doc is not None and doc.author_external_id is None


def test_issue_comment_hash_ignores_reactions(repo):
    c = load("issue_comment")
    before = normalize_issue_comment(c, repo).content_hash
    c["reactions"]["heart"] = 5
    c["updated_at"] = "2025-01-01T00:00:00Z"
    assert normalize_issue_comment(c, repo).content_hash == before
    c["body"] = "Changed my mind."
    assert normalize_issue_comment(c, repo).content_hash != before


# --- PR reviews -------------------------------------------------------------


def _review(**overrides) -> dict:
    r = load("pr_review")
    r.update(overrides)
    return r


def test_review_without_body_is_skipped(repo):
    assert load("pr_review")["body"] is None  # the octokit example is a bare review
    assert normalize_pr_review(load("pr_review"), 2, repo) is None
    assert normalize_pr_review(_review(body="  \r\n\t "), 2, repo) is None


def test_review_with_body(repo):
    doc = normalize_pr_review(_review(body="Please split this PR.", state="commented"), 2, repo)
    assert doc is not None
    assert doc.kind == "pr_review"
    assert doc.external_id == "MDE3OlB1bGxSZXF1ZXN0UmV2aWV3MjM3ODk1Njcx"
    assert doc.url == "https://github.com/Codertocat/Hello-World/pull/2#pullrequestreview-237895671"
    assert doc.body == "Please split this PR."
    assert doc.metadata["state"] == "COMMENTED"  # webhook lowercase -> REST uppercase
    assert doc.metadata["parent_number"] == 2
    assert doc.metadata["commit_id"] == "ec26c3e57ca3a959ca5aad62de7213c562f8c821"
    assert doc.source_created_at == datetime(2019, 5, 15, 15, 20, 38, tzinfo=UTC)
    assert doc.hash_fields == {"state": "COMMENTED"}


def test_review_skips_pending_and_bots(repo):
    assert normalize_pr_review(_review(body="draft", state="PENDING"), 2, repo) is None
    assert normalize_pr_review(_review(body="draft", submitted_at=None), 2, repo) is None
    assert normalize_pr_review(_review(body="LGTM", user=DEPENDABOT), 2, repo) is None


# --- approvals ---------------------------------------------------------------


def _r(login: str, state: str, at: str, user_type: str = "User") -> dict:
    return {
        "user": {"login": login, "id": hash(login) % 10_000, "type": user_type},
        "state": state,
        "submitted_at": f"2024-03-01T{at}:00Z",
    }


def test_approvals_basic():
    reviews = [_r("bob", "APPROVED", "10:00"), _r("alice", "APPROVED", "09:00")]
    assert approvals(reviews) == ["alice", "bob"]


def test_approvals_changes_requested_cancels():
    reviews = [_r("alice", "APPROVED", "09:00"), _r("alice", "CHANGES_REQUESTED", "10:00")]
    assert approvals(reviews) == []


def test_approvals_reapproval_after_changes_requested():
    reviews = [
        _r("alice", "APPROVED", "09:00"),
        _r("alice", "CHANGES_REQUESTED", "10:00"),
        _r("alice", "APPROVED", "11:00"),
    ]
    assert approvals(reviews) == ["alice"]


def test_approvals_comment_after_approval_does_not_cancel():
    reviews = [_r("alice", "APPROVED", "09:00"), _r("alice", "COMMENTED", "10:00")]
    assert approvals(reviews) == ["alice"]


def test_approvals_dismissed_cancels_and_order_is_by_time():
    reviews = [
        _r("carol", "DISMISSED", "12:00"),
        _r("carol", "APPROVED", "08:00"),  # listed out of order on purpose
        _r("dave", "approved", "08:00"),  # webhook-style lowercase
    ]
    assert approvals(reviews) == ["dave"]


def test_approvals_ignores_bots_ghosts_and_pending():
    reviews = [
        _r("renovate[bot]", "APPROVED", "09:00", user_type="Bot"),
        {"user": None, "state": "APPROVED", "submitted_at": "2024-03-01T09:00:00Z"},
        {"user": {"login": "erin", "id": 5}, "state": "PENDING", "submitted_at": None},
    ]
    assert approvals(reviews) == []
    assert approvals([]) == []


# --- PR review comments -----------------------------------------------------


def test_pr_review_comment(repo):
    doc = normalize_pr_review_comment(load("pr_review_comment"), repo)
    assert doc is not None
    assert doc.kind == "pr_review_comment"
    assert doc.external_id == "MDI0OlB1bGxSZXF1ZXN0UmV2aWV3Q29tbWVudDI4NDMxMjYzMA=="
    assert doc.url == "https://github.com/Codertocat/Hello-World/pull/2#discussion_r284312630"
    assert doc.body == "Maybe you should use more emoji on this line."
    assert doc.metadata["path"] == "README.md"
    assert doc.metadata["line"] == 265
    assert doc.metadata["side"] == "RIGHT"
    assert doc.metadata["parent_number"] == 2
    assert doc.metadata["in_reply_to_id"] is None
    assert doc.metadata["diff_hunk"] == "@@ -1 +1 @@\n-# Hello-World"
    assert doc.hash_fields == {"path": "README.md"}


def test_pr_review_comment_outdated_uses_original_line_and_reply(repo):
    c = load("pr_review_comment")
    c["line"] = None
    c["original_line"] = 12
    c["in_reply_to_id"] = 284312000
    doc = normalize_pr_review_comment(c, repo)
    assert doc.metadata["line"] == 12
    assert doc.metadata["in_reply_to_id"] == 284312000


def test_diff_hunk_trimmed_to_last_20_lines(repo):
    c = load("pr_review_comment")
    lines = ["@@ -1,40 +1,40 @@"] + [f"+line {i}" for i in range(1, 41)]
    c["diff_hunk"] = "\n".join(lines)
    doc = normalize_pr_review_comment(c, repo)
    kept = doc.metadata["diff_hunk"].split("\n")
    assert len(kept) == 20
    assert kept == lines[-20:]
    assert kept[-1] == "+line 40"


def test_pr_review_comment_hash_includes_path(repo):
    c = load("pr_review_comment")
    before = normalize_pr_review_comment(c, repo).content_hash
    c["updated_at"] = "2030-01-01T00:00:00Z"
    c["line"] = 300  # line moves with new pushes; not content
    assert normalize_pr_review_comment(c, repo).content_hash == before
    c["path"] = "docs/README.md"
    assert normalize_pr_review_comment(c, repo).content_hash != before


def test_pr_review_comment_bot_skipped(repo):
    c = load("pr_review_comment")
    c["user"] = DEPENDABOT
    assert normalize_pr_review_comment(c, repo) is None


# --- markdown files ---------------------------------------------------------


ADR = """---
status: accepted
---

```bash
# not a title
```

# ADR 0003: Use Postgres for search\r
\r
We chose Postgres FTS + pgvector over Elasticsearch.   \r
"""


def test_markdown_file(repo):
    doc = normalize_markdown_file(
        repo=repo,
        path="docs/adr/0003-postgres.md",
        content=ADR,
        blob_sha="b" * 40,
        commit_sha="c" * 40,
    )
    assert doc.kind == "markdown_file"
    assert doc.external_id == "186853002:docs/adr/0003-postgres.md"
    assert doc.title == "ADR 0003: Use Postgres for search"
    assert doc.url == (
        f"https://github.com/Codertocat/Hello-World/blob/{'c' * 40}/docs/adr/0003-postgres.md"
    )
    assert "\r" not in doc.body
    assert doc.body.endswith("over Elasticsearch.")
    assert doc.author_external_id is None and doc.author_login is None
    assert doc.source_created_at is None and doc.source_updated_at is None
    assert doc.metadata == {"path": "docs/adr/0003-postgres.md", "blob_sha": "b" * 40}
    assert doc.hash_fields == {"path": "docs/adr/0003-postgres.md"}


def test_markdown_file_defaults(repo):
    doc = normalize_markdown_file(
        repo=repo, path="README.md", content="No heading here.", blob_sha="a1", commit_sha=None
    )
    assert doc.title == "README.md"
    assert doc.url == "https://github.com/Codertocat/Hello-World/blob/master/README.md"


def test_markdown_file_hash_and_truncation(repo):
    kw = {"repo": repo, "path": "docs/a.md", "commit_sha": None}
    a = normalize_markdown_file(content="# A\nbody", blob_sha="1", **kw)
    b = normalize_markdown_file(content="# A\r\nbody  ", blob_sha="2", **kw)
    assert a.content_hash == b.content_hash  # blob sha isn't content; CRLF normalized
    moved = normalize_markdown_file(
        repo=repo, path="docs/b.md", content="# A\nbody", blob_sha="1", commit_sha=None
    )
    assert moved.content_hash != a.content_hash
    big = normalize_markdown_file(content="y" * (MAX_BODY_BYTES + 1), blob_sha="3", **kw)
    assert big.metadata["truncated"] is True


# --- bots and globs ---------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (DEPENDABOT, True),
        ({"login": "github-actions[bot]", "type": "User"}, True),
        ({"login": "some-app", "type": "Bot"}, True),
        ({"login": "Codertocat", "type": "User"}, False),
        ({"login": "botanist", "type": "User"}, False),
        (None, False),
    ],
)
def test_is_bot(user, expected):
    assert is_bot(user) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("README.md", True),
        ("readme.MD", True),
        ("src/README.md", False),  # only the root README by default
        ("docs/intro.md", True),
        ("docs/guides/deep/setup.md", True),
        ("Docs/Intro.MD", True),
        ("docs/diagram.png", False),
        ("adr/0001-record.md", True),
        ("architecture/adr/0002-graphiti.md", True),
        ("services/api/ADR-0004-celery.md", True),
        ("ADR-0005.md", True),
        ("rfcs/0001-mcp.md", True),
        ("platform/rfcs/2024/auth.md", True),
        ("notdocs/readme.md", False),
        ("CHANGELOG.md", False),
        ("src/main.py", False),
    ],
)
def test_default_doc_globs(path, expected):
    assert matches_doc_globs(path, None) is expected


def test_custom_doc_globs():
    assert matches_doc_globs("CHANGELOG.md", ["*.md"]) is True
    assert matches_doc_globs("a/CHANGELOG.md", ["*.md"]) is False
    assert matches_doc_globs("a/CHANGELOG.md", ["**/*.md"]) is True
    assert matches_doc_globs("README.md", []) is False
    assert "README.md" in DEFAULT_DOC_GLOBS
