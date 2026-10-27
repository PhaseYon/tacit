"""GitHub source: one repo per scope entry, synced with the user's own token.

Items (one per unit of meaning, see tacit.sources.base):
- a PR or issue, rendered with its human comments (and for PRs: reviews, inline review
  comments, approvals) as one transcript
- a markdown doc on the default branch (README, docs/, ADRs, RFCs)

Cursor (JSON, opaque to the orchestrator):
    {"since": "<updated_at of the last issue/PR handled>",
     "seen": [node ids with exactly that updated_at, already handled],
     "docs_tree": "<default-branch tree sha at the last finished docs pass>",
     "doc_blobs": {"<path>": "<blob sha>"}}

API calls stay proportional to what changed: the issues listing is filtered by `since`,
items already handled at the boundary timestamp are skipped without extra calls, the
docs pass is one small branch lookup when the tree is unchanged, and only doc blobs
whose sha moved are downloaded.
"""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from githubkit import GitHub
from githubkit.exception import RateLimitExceeded, RequestFailed

from ...config import Config
from ..base import Item, Page, ScopeEntry, SourceAccessDenied
from . import api
from .normalize import (
    approvals,
    matches_doc_globs,
    normalize_issue,
    normalize_issue_comment,
    normalize_markdown_file,
    normalize_pr_review,
    normalize_pr_review_comment,
    parse_ts,
)
from .render import Bundle, Member, render

log = logging.getLogger(__name__)

PER_PAGE = 100
DOCS_PER_PAGE = 50
CONCURRENCY = 8


def _status(exc: RequestFailed) -> int:
    return exc.response.status_code


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHubSource:
    name = "github"

    def __init__(self, token: str, config: Config) -> None:
        self.config = config
        # auto_retry: wait out rate limits, retry 5xx. http_cache: in-memory ETag cache.
        self.gh = GitHub(
            token,
            base_url=config.sources.github_api_url,
            auto_retry=True,
            http_cache=True,
        )
        self._sem = asyncio.Semaphore(CONCURRENCY)

    # -- resolve -----------------------------------------------------------------------

    async def resolve(self, target: str) -> ScopeEntry:
        full = target.strip().removeprefix("https://github.com/").strip("/")
        full = full.removesuffix(".git")
        if full.count("/") != 1 or not all(full.split("/")):
            raise ValueError(f"Expected a GitHub repo as owner/repo, got {target!r}")
        try:
            repo = await api.get_json(self.gh, f"/repos/{full}")
        except RateLimitExceeded:
            raise
        except RequestFailed as exc:
            if _status(exc) in (401, 403, 404):
                raise ValueError(
                    f"GitHub repo {full} not found, or your token can't read it "
                    f"(HTTP {_status(exc)}). Check the name and the token's repo access."
                ) from exc
            raise
        name = repo["full_name"]
        return ScopeEntry(
            key=f"github:{name}",
            source="github",
            target=name,
            label=name,
            options={"private": bool(repo.get("private"))},
        )

    # -- fetch -------------------------------------------------------------------------

    async def fetch(self, entry: ScopeEntry, cursor: str | None) -> AsyncIterator[Page]:
        state: dict[str, Any] = json.loads(cursor) if cursor else {}
        try:
            repo = await api.get_json(self.gh, f"/repos/{entry.target}")
        except RateLimitExceeded:
            raise
        except RequestFailed as exc:
            if _status(exc) in (401, 403, 404):
                raise SourceAccessDenied(
                    f"your GitHub token can't read {entry.target} (HTTP {_status(exc)})"
                ) from exc
            raise

        async for page in self._fetch_issues(entry, repo, state):
            yield page
        async for page in self._fetch_docs(entry, repo, state):
            yield page

    async def _fetch_issues(
        self, entry: ScopeEntry, repo: dict, state: dict[str, Any]
    ) -> AsyncIterator[Page]:
        since = state.get("since") or _iso(
            datetime.now(UTC) - timedelta(days=self.config.sources.backfill_days)
        )
        seen = set(state.get("seen") or [])
        url: str | None = f"/repos/{repo['full_name']}/issues"
        params: dict[str, Any] | None = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "since": since,
            "per_page": PER_PAGE,
        }
        while url:
            raw, url = await api.get_page(self.gh, url, params)
            params = None  # the Link header's next URL carries them
            fresh = [i for i in raw if not (i.get("updated_at") == since and i["node_id"] in seen)]
            built = await asyncio.gather(*(self._issue_item(entry, repo, i) for i in fresh))
            for i in raw:  # sorted by updated_at ascending
                ts = i.get("updated_at")
                if not ts:
                    continue
                if ts != since:
                    since, seen = ts, set()
                seen.add(i["node_id"])
            state["since"], state["seen"] = since, sorted(seen)
            yield Page([b for b in built if b is not None], json.dumps(state, sort_keys=True))

    async def _issue_item(self, entry: ScopeEntry, repo: dict, issue: dict) -> Item | None:
        root = normalize_issue(issue, repo)
        if root is None:  # bot-authored
            return None
        full, number = repo["full_name"], issue["number"]
        is_pr = root.kind == "pull_request"
        members = [_member(root)]
        reviews: list[dict] = []

        async with self._sem:
            comment_calls = []
            if issue.get("comments"):
                comment_calls.append(self._all(f"/repos/{full}/issues/{number}/comments"))
            if is_pr:
                comment_calls.append(self._all(f"/repos/{full}/pulls/{number}/reviews"))
                comment_calls.append(self._all(f"/repos/{full}/pulls/{number}/comments"))
            results = await asyncio.gather(*comment_calls)

        comments = results[0] if issue.get("comments") else []
        if is_pr:
            reviews, inline = results[-2], results[-1]
        else:
            inline = []

        human_comments = [
            d for d in (normalize_issue_comment(c, repo) for c in comments) if d is not None
        ]
        others = [
            *(normalize_pr_review(r, number, repo) for r in reviews),
            *(normalize_pr_review_comment(c, repo) for c in inline),
        ]
        children = human_comments + [d for d in others if d is not None]
        children.sort(key=lambda d: d.source_created_at or datetime.min.replace(tzinfo=UTC))
        members += [_member(d) for d in children]

        meta = dict(root.metadata)
        meta["repo"] = full
        if is_pr:
            meta["approved_by"] = approvals(reviews)
        bundle = Bundle(full, root.kind, root.title, meta, members)

        merged = bundle.state == "merged"
        if merged:
            ref_time = parse_ts(meta["merged_at"])
        elif issue.get("state") == "closed":
            ref_time = parse_ts(issue.get("closed_at")) or root.source_updated_at
        else:
            ref_time = root.source_updated_at
        if is_pr:
            extract = merged
        else:
            extract = issue.get("state") == "closed" and len(human_comments) >= 2

        return Item(
            source="github",
            scope_key=entry.key,
            external_id=f"github:{root.external_id}",
            kind=root.kind,
            url=root.url,
            title=root.title or f"#{number}",
            text=render(bundle),
            reference_time=ref_time,
            participants=tuple(f"github:{p}" for p in bundle.participants),
            metadata=meta,
            extract=extract,
        )

    async def _fetch_docs(
        self, entry: ScopeEntry, repo: dict, state: dict[str, Any]
    ) -> AsyncIterator[Page]:
        full, branch = repo["full_name"], repo["default_branch"]
        try:
            head = await api.get_json(self.gh, f"/repos/{full}/branches/{branch}")
        except RequestFailed as exc:
            if _status(exc) in (404, 409):  # empty repository
                return
            raise
        commit = head["commit"]["commit"]
        tree_sha = commit["tree"]["sha"]
        if tree_sha == state.get("docs_tree"):
            return
        committed_at = parse_ts((commit.get("committer") or {}).get("date"))

        tree = await api.get_json(
            self.gh, f"/repos/{full}/git/trees/{tree_sha}", {"recursive": "1"}
        )
        if tree.get("truncated"):
            log.warning("%s: git tree is truncated; some docs may be missed", full)
        known: dict[str, str] = dict(state.get("doc_blobs") or {})
        docs = {
            t["path"]: t["sha"]
            for t in tree.get("tree", [])
            if t.get("type") == "blob" and matches_doc_globs(t["path"], None)
        }
        known = {p: s for p, s in known.items() if p in docs}  # forget deleted files
        changed = sorted(p for p, sha in docs.items() if known.get(p) != sha)

        for start in range(0, len(changed), DOCS_PER_PAGE):
            batch = changed[start : start + DOCS_PER_PAGE]
            items = await asyncio.gather(
                *(self._doc_item(entry, repo, p, docs[p], committed_at) for p in batch)
            )
            known.update({p: docs[p] for p in batch})
            state["doc_blobs"] = known
            if start + DOCS_PER_PAGE >= len(changed):
                state["docs_tree"] = tree_sha
            yield Page(list(items), json.dumps(state, sort_keys=True))
        if not changed:
            state["doc_blobs"], state["docs_tree"] = known, tree_sha
            yield Page([], json.dumps(state, sort_keys=True))

    async def _doc_item(
        self, entry: ScopeEntry, repo: dict, path: str, blob_sha: str, at: datetime | None
    ) -> Item:
        full = repo["full_name"]
        async with self._sem:
            blob = await api.get_json(self.gh, f"/repos/{full}/git/blobs/{blob_sha}")
        content = base64.b64decode(blob.get("content") or "").decode("utf-8", errors="replace")
        doc = normalize_markdown_file(
            repo=repo, path=path, content=content, blob_sha=blob_sha, commit_sha=None
        )
        meta = {**doc.metadata, "repo": full}
        bundle = Bundle(full, doc.kind, doc.title, meta, [_member(doc)])
        return Item(
            source="github",
            scope_key=entry.key,
            external_id=f"github:{doc.external_id}",
            kind=doc.kind,
            url=doc.url,
            title=doc.title or path,
            text=render(bundle),
            reference_time=at,
            participants=(),
            metadata=meta,
            extract=True,
        )

    async def _all(self, url: str) -> list[Any]:
        return await api.get_all(self.gh, url, {"per_page": PER_PAGE})


def _member(doc) -> Member:
    return Member(
        kind=doc.kind,
        author_login=doc.author_login,
        created_at=doc.source_created_at,
        body=doc.body,
        metadata=doc.metadata,
    )
