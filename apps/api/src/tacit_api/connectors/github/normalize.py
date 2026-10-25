"""Pure functions: GitHub JSON (REST responses or webhook payload objects) -> NormalizedDocument.

No I/O here. Every function takes plain dicts and returns a NormalizedDocument, or None when
the item should be skipped (bot authors, empty reviews, ...). See docs/design/github-connector.md
sections 3 and 5 for what we ingest and what goes into the content hash.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from wcmatch import glob

from tacit_api.connectors.documents import NormalizedDocument, normalize_body

DEFAULT_DOC_GLOBS: tuple[str, ...] = (
    "README.md",
    "docs/**/*.md",
    "**/adr/**/*.md",
    "**/ADR-*.md",
    "**/rfcs/**/*.md",
)

DIFF_HUNK_MAX_LINES = 20

# GLOBSTAR: `**` spans directories (and `**/` also matches zero of them).
# IGNORECASE: `README.MD`, `Docs/` and `.MD` count too.
_GLOB_FLAGS = glob.GLOBSTAR | glob.IGNORECASE


def is_bot(user: dict | None) -> bool:
    if not user:
        return False
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def matches_doc_globs(path: str, globs: list[str] | None) -> bool:
    """True if `path` (repo-relative, `/`-separated) is a doc we ingest. None = defaults."""
    patterns = DEFAULT_DOC_GLOBS if globs is None else globs
    if not patterns:
        return False
    return glob.globmatch(path.lstrip("/"), list(patterns), flags=_GLOB_FLAGS)


def approvals(reviews: list[dict]) -> list[str]:
    """Logins whose latest decisive review is APPROVED, sorted.

    COMMENTED and PENDING reviews never change someone's standing; a later CHANGES_REQUESTED
    or DISMISSED cancels an earlier approval, and a later APPROVED restores it. Bots and
    deleted users are ignored.
    """
    indexed = [
        (r.get("submitted_at") or "", i, r)
        for i, r in enumerate(reviews)
        if r.get("user") and not is_bot(r["user"])
    ]
    latest: dict[str, str] = {}
    # ISO-8601 UTC strings sort chronologically; the index keeps API order for ties.
    for _, _, review in sorted(indexed, key=lambda t: (t[0], t[1])):
        state = str(review.get("state") or "").upper()
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[review["user"]["login"]] = state
    return sorted(login for login, state in latest.items() if state == "APPROVED")


def normalize_issue(issue: dict, repo: dict) -> NormalizedDocument | None:
    """An issue, a PR from the /issues listing (has `pull_request`), or a full PR object."""
    if is_bot(issue.get("user")):
        return None

    is_full_pr = "head" in issue and "base" in issue
    is_pr = is_full_pr or "pull_request" in issue
    if is_full_pr:
        merged_at = issue.get("merged_at")
    elif is_pr:
        merged_at = (issue.get("pull_request") or {}).get("merged_at")
    else:
        merged_at = None
    merged = bool(merged_at) or (is_full_pr and bool(issue.get("merged")))

    body, truncated = normalize_body(issue.get("body"))
    labels = _label_names(issue.get("labels"))
    author_id, author_login = _author(issue.get("user"))

    metadata: dict[str, Any] = {
        "number": issue["number"],
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "labels": labels,
        "merged_at": merged_at,
        "draft": bool(issue.get("draft")),
        "assignees": sorted(a["login"] for a in issue.get("assignees") or [] if a),
    }
    if is_full_pr:
        metadata["base_ref"] = (issue.get("base") or {}).get("ref")
        metadata["head_ref"] = (issue.get("head") or {}).get("ref")
    if truncated:
        metadata["truncated"] = True

    return NormalizedDocument(
        kind="pull_request" if is_pr else "issue",
        external_id=issue["node_id"],
        url=issue["html_url"],
        title=issue.get("title"),
        body=body,
        author_external_id=author_id,
        author_login=author_login,
        source_created_at=parse_ts(issue.get("created_at")),
        source_updated_at=parse_ts(issue.get("updated_at")),
        metadata=metadata,
        hash_fields={
            "state": issue.get("state"),
            "state_reason": issue.get("state_reason"),
            "labels": labels,
            "merged": merged,
        },
    )


def normalize_issue_comment(comment: dict, repo: dict) -> NormalizedDocument | None:
    """A conversation comment on an issue or PR. The sync layer resolves parent_number."""
    if is_bot(comment.get("user")):
        return None
    body, truncated = normalize_body(comment.get("body"))
    author_id, author_login = _author(comment.get("user"))
    metadata: dict[str, Any] = {
        "parent_number": _number_from_url(comment.get("issue_url")),
        "comment_id": comment.get("id"),
    }
    if truncated:
        metadata["truncated"] = True
    return NormalizedDocument(
        kind="issue_comment",
        external_id=comment["node_id"],
        url=comment["html_url"],
        title=None,
        body=body,
        author_external_id=author_id,
        author_login=author_login,
        source_created_at=parse_ts(comment.get("created_at")),
        source_updated_at=parse_ts(comment.get("updated_at")),
        parent_external_id=None,
        metadata=metadata,
    )


def normalize_pr_review(review: dict, pr_number: int, repo: dict) -> NormalizedDocument | None:
    """A submitted PR review with text. Bare approvals and pending drafts are skipped."""
    if is_bot(review.get("user")):
        return None
    state = str(review.get("state") or "").upper()  # webhooks send lowercase, REST uppercase
    if state == "PENDING" or not review.get("submitted_at"):
        return None
    body, truncated = normalize_body(review.get("body"))
    if not body:
        return None
    author_id, author_login = _author(review.get("user"))
    submitted_at = parse_ts(review.get("submitted_at"))
    metadata: dict[str, Any] = {
        "state": state,
        "parent_number": pr_number,
        "commit_id": review.get("commit_id"),
        "review_id": review.get("id"),
    }
    if truncated:
        metadata["truncated"] = True
    return NormalizedDocument(
        kind="pr_review",
        external_id=review["node_id"],
        url=review["html_url"],
        title=None,
        body=body,
        author_external_id=author_id,
        author_login=author_login,
        # Reviews only carry submitted_at; body edits don't move it.
        source_created_at=submitted_at,
        source_updated_at=submitted_at,
        parent_external_id=None,
        metadata=metadata,
        hash_fields={"state": state},
    )


def normalize_pr_review_comment(comment: dict, repo: dict) -> NormalizedDocument | None:
    """An inline diff comment on a PR, with the last few lines of its diff hunk."""
    if is_bot(comment.get("user")):
        return None
    body, truncated = normalize_body(comment.get("body"))
    author_id, author_login = _author(comment.get("user"))
    line = comment.get("line")
    if line is None:  # outdated comments lose `line` but keep `original_line`
        line = comment.get("original_line")
    metadata: dict[str, Any] = {
        "path": comment.get("path"),
        "line": line,
        "side": comment.get("side"),
        "parent_number": _number_from_url(comment.get("pull_request_url")),
        "in_reply_to_id": comment.get("in_reply_to_id"),
        "review_id": comment.get("pull_request_review_id"),
        "comment_id": comment.get("id"),
        "diff_hunk": _tail_lines(comment.get("diff_hunk"), DIFF_HUNK_MAX_LINES),
    }
    if truncated:
        metadata["truncated"] = True
    return NormalizedDocument(
        kind="pr_review_comment",
        external_id=comment["node_id"],
        url=comment["html_url"],
        title=None,
        body=body,
        author_external_id=author_id,
        author_login=author_login,
        source_created_at=parse_ts(comment.get("created_at")),
        source_updated_at=parse_ts(comment.get("updated_at")),
        parent_external_id=None,
        metadata=metadata,
        hash_fields={"path": comment.get("path")},
    )


def normalize_markdown_file(
    *, repo: dict, path: str, content: str, blob_sha: str, commit_sha: str | None
) -> NormalizedDocument:
    """A markdown doc on the default branch. Files have no author or timestamps."""
    body, truncated = normalize_body(content)
    ref = commit_sha or repo["default_branch"]
    metadata: dict[str, Any] = {"path": path, "blob_sha": blob_sha}
    if truncated:
        metadata["truncated"] = True
    return NormalizedDocument(
        kind="markdown_file",
        external_id=f"{repo['id']}:{path}",
        url=f"{repo['html_url']}/blob/{quote(ref, safe='')}/{quote(path, safe='/')}",
        title=_markdown_title(body) or path.rsplit("/", 1)[-1],
        body=body,
        author_external_id=None,
        author_login=None,
        source_created_at=None,
        source_updated_at=None,
        metadata=metadata,
        hash_fields={"path": path},
    )


# --- helpers ---------------------------------------------------------------


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _author(user: dict | None) -> tuple[str | None, str | None]:
    if not user:  # deleted ("ghost") accounts come through as null
        return None, None
    return str(user["id"]), user.get("login")


def _label_names(labels: Iterable[Any] | None) -> list[str]:
    names = (lb if isinstance(lb, str) else lb.get("name") for lb in labels or [] if lb)
    return sorted(n for n in names if n)


def _number_from_url(url: str | None) -> int | None:
    """`.../issues/12` or `.../pulls/12` -> 12."""
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _tail_lines(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(lines[-n:])


def _markdown_title(body: str) -> str | None:
    """First ATX `# ` heading outside fenced code blocks."""
    fence: str | None = None
    for raw in body.split("\n"):
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            marker = line[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is None and line.startswith("# "):
            title = line[2:].strip().rstrip("#").strip()
            if title:
                return title
    return None
