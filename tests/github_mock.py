"""A small in-memory GitHub REST API for tests (respx), plus JSON builders.

FakeGitHub keeps repos, issues/PRs, comments, reviews and a default-branch tree, and
serves the endpoints GitHubSource uses: repo lookup, the `since`-filtered issues
listing (paginated with Link headers), comments/reviews, branch, tree and blobs.
Every request is recorded in `calls` so tests can assert API usage stays proportional.
"""

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import respx

API = "https://api.github.com"


def repo_json(repo_id: int, name: str, *, private: bool = True, owner: str = "acme") -> dict:
    full = f"{owner}/{name}"
    return {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "name": name,
        "full_name": full,
        "private": private,
        "visibility": "private" if private else "public",
        "html_url": f"https://github.com/{full}",
        "default_branch": "main",
        "archived": False,
    }


def user(login: str) -> dict:
    bot = login.endswith("[bot]")
    return {"login": login, "id": abs(hash(login)) % 10**8, "type": "Bot" if bot else "User"}


def _ts(day: int, hour: int = 0) -> str:
    return f"2026-03-{day:02d}T{hour:02d}:00:00Z"


@dataclass
class FakeGitHub:
    repo: dict
    issues: dict[int, dict] = field(default_factory=dict)
    comments: dict[int, list[dict]] = field(default_factory=dict)
    reviews: dict[int, list[dict]] = field(default_factory=dict)
    inline: dict[int, list[dict]] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # path -> content on main
    page_size: int = 2
    denied: bool = False
    calls: list[str] = field(default_factory=list)
    _ids: int = 1000

    # -- builders ----------------------------------------------------------------------

    def _next_id(self) -> int:
        self._ids += 1
        return self._ids

    def add_issue(
        self,
        number: int,
        title: str,
        author: str,
        *,
        updated: str,
        body: str = "",
        state: str = "open",
        closed_at: str | None = None,
        pr: bool = False,
        merged_at: str | None = None,
        labels: tuple[str, ...] = (),
        draft: bool = False,
    ) -> dict:
        html = f"{self.repo['html_url']}/{'pull' if pr else 'issues'}/{number}"
        issue: dict[str, Any] = {
            "id": self._next_id(),
            "node_id": f"{'PR' if pr else 'I'}_{number}",
            "number": number,
            "title": title,
            "body": body,
            "user": user(author),
            "state": state,
            "state_reason": None,
            "labels": [{"name": n} for n in labels],
            "assignees": [],
            "comments": 0,
            "created_at": _ts(1),
            "updated_at": updated,
            "closed_at": closed_at,
            "html_url": html,
            "draft": draft,
        }
        if pr:
            issue["pull_request"] = {"merged_at": merged_at, "html_url": html}
        self.issues[number] = issue
        return issue

    def add_comment(self, number: int, author: str, body: str, at: str = _ts(2)) -> None:
        cid = self._next_id()
        self.comments.setdefault(number, []).append(
            {
                "id": cid,
                "node_id": f"IC_{cid}",
                "user": user(author),
                "body": body,
                "created_at": at,
                "updated_at": at,
                "html_url": f"{self.issues[number]['html_url']}#issuecomment-{cid}",
                "issue_url": f"{API}/repos/{self.repo['full_name']}/issues/{number}",
            }
        )
        self.issues[number]["comments"] = len(self.comments[number])

    def add_review(self, number: int, author: str, state: str, body: str = "", at: str = _ts(3)):
        rid = self._next_id()
        self.reviews.setdefault(number, []).append(
            {
                "id": rid,
                "node_id": f"PRR_{rid}",
                "user": user(author),
                "body": body,
                "state": state,
                "submitted_at": at,
                "commit_id": "abc",
                "html_url": f"{self.issues[number]['html_url']}#pullrequestreview-{rid}",
            }
        )

    def add_inline(self, number: int, author: str, path: str, body: str, at: str = _ts(3, 1)):
        cid = self._next_id()
        self.inline.setdefault(number, []).append(
            {
                "id": cid,
                "node_id": f"PRRC_{cid}",
                "user": user(author),
                "body": body,
                "path": path,
                "line": 3,
                "side": "RIGHT",
                "diff_hunk": "@@ -1 +1 @@\n-a\n+b",
                "created_at": at,
                "updated_at": at,
                "html_url": f"{self.issues[number]['html_url']}#discussion_r{cid}",
                "pull_request_url": f"{API}/repos/{self.repo['full_name']}/pulls/{number}",
            }
        )

    # -- serving -----------------------------------------------------------------------

    def mount(self, router: respx.MockRouter, base: str = API) -> None:
        router.route(host=urlparse(base).hostname).mock(side_effect=self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        full = self.repo["full_name"]
        if self.denied or not path.startswith(f"/repos/{full}"):
            return httpx.Response(404, json={"message": "Not Found"})
        rest = path[len(f"/repos/{full}") :]
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if rest == "":
            return httpx.Response(200, json=self.repo)
        if rest == "/issues":
            listed = sorted(
                (i for i in self.issues.values() if i["updated_at"] >= query.get("since", "")),
                key=lambda i: i["updated_at"],
            )
            return self._paged(request, listed, query)
        if m := re.fullmatch(r"/issues/(\d+)/comments", rest):
            return self._paged(request, self.comments.get(int(m[1]), []), query)
        if m := re.fullmatch(r"/pulls/(\d+)/reviews", rest):
            return self._paged(request, self.reviews.get(int(m[1]), []), query)
        if m := re.fullmatch(r"/pulls/(\d+)/comments", rest):
            return self._paged(request, self.inline.get(int(m[1]), []), query)
        if rest == f"/branches/{self.repo['default_branch']}":
            return httpx.Response(
                200,
                json={
                    "name": self.repo["default_branch"],
                    "commit": {
                        "sha": "c" * 40,
                        "commit": {
                            "tree": {"sha": self.tree_sha},
                            "committer": {"date": _ts(5)},
                        },
                    },
                },
            )
        if rest == f"/git/trees/{self.tree_sha}":
            tree = [{"path": p, "type": "blob", "sha": _sha(c)} for p, c in self.files.items()]
            return httpx.Response(200, json={"sha": self.tree_sha, "tree": tree})
        if m := re.fullmatch(r"/git/blobs/(\w+)", rest):
            for content in self.files.values():
                if _sha(content) == m[1]:
                    enc = base64.b64encode(content.encode()).decode()
                    return httpx.Response(200, json={"content": enc, "encoding": "base64"})
        return httpx.Response(404, json={"message": f"unmocked {path}"})

    def _paged(self, request: httpx.Request, items: list, query: dict) -> httpx.Response:
        page = int(query.get("page", "1"))
        chunk = items[(page - 1) * self.page_size : page * self.page_size]
        headers = {}
        if page * self.page_size < len(items):
            q = {**query, "page": str(page + 1)}
            nxt = request.url.copy_with(query=urlencode(q).encode())
            headers["Link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    @property
    def tree_sha(self) -> str:
        return _sha("|".join(f"{p}={c}" for p, c in sorted(self.files.items())))

    def count(self, pattern: str) -> int:
        return sum(1 for c in self.calls if re.search(pattern, c))


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()
