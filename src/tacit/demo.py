"""`tacit demo load`: the fictional Larkspur Labs company, loaded through the real sync path.

The demo data (demo/data/github/*.json) uses GitHub REST API shapes. Instead of a separate
loader, an in-process fake GitHub API serves it to the real GitHubSource, so the demo
exercises exactly the code real teams run: same rendering, chunking, hash skip, cursors
and (optionally) extraction. No network calls, no randomness.

Data comes from a checkout's demo/ folder, or is downloaded from the repo when tacit was
installed as a package.
"""

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
from githubkit import GitHub

from .config import Config, get_config
from .sources.base import ScopeEntry
from .sources.github.source import GitHubSource

REPOS = ("handbook", "api", "web")
OWNER = "larkspur"
RAW_URL = "https://raw.githubusercontent.com/PhaseYon/tacit/main/demo/data/github/{name}.json"
PER_PAGE_MAX = 100


def default_data_dir() -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / "demo" / "data" / "github"
    return candidate if candidate.is_dir() else None


def load_data(data_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """repo full_name -> demo JSON."""
    out = {}
    for name in REPOS:
        if data_dir or (data_dir := default_data_dir()):
            data = json.loads((data_dir / f"{name}.json").read_text())
        else:
            resp = httpx.get(RAW_URL.format(name=name), timeout=30, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        out[data["repository"]["full_name"]] = data
    return out


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


_DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")


def _file_date(content: str) -> str | None:
    """The generated demo has no git history, so stand in for "last commit that touched
    this file" with the latest date in the doc's header (ADR "Date:", "Superseded by …
    (date)", "Last updated …"). Real repos use the GitHub commits API instead."""
    dates = _DATE.findall("\n".join(content.splitlines()[:12]))
    return f"{max(dates)}T09:00:00Z" if dates else None


def _number(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


class DemoGitHubAPI:
    """Just enough of the GitHub REST API for GitHubSource, served from the demo JSON."""

    def __init__(self, repos: dict[str, dict[str, Any]]) -> None:
        self.repos = repos

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(.*)", request.url.path)
        if not m or m[1] not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"})
        data, rest = self.repos[m[1]], m[2]
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        repo = data["repository"]
        if rest == "":
            return httpx.Response(200, json=repo)
        if rest == "/issues":
            since = query.get("since", "")
            listed = sorted(
                (i for i in data["issues"] + data["pull_requests"] if i["updated_at"] >= since),
                key=lambda i: (i["updated_at"], i["number"]),
            )
            return self._paged(request, listed, query)
        if c := re.fullmatch(r"/issues/(\d+)/comments", rest):
            n = int(c[1])
            items = [x for x in data["issue_comments"] if _number(x["issue_url"]) == n]
            return self._paged(request, items, query)
        if c := re.fullmatch(r"/pulls/(\d+)/reviews", rest):
            n = int(c[1])
            items = [r for r in data["pull_request_reviews"] if _number(r["pull_request_url"]) == n]
            return self._paged(request, items, query)
        if c := re.fullmatch(r"/pulls/(\d+)/comments", rest):
            n = int(c[1])
            items = [x for x in data["review_comments"] if _number(x["pull_request_url"]) == n]
            return self._paged(request, items, query)
        files = {f["path"]: f["content"] for f in data["markdown_files"]}
        tree_sha = _sha("|".join(f"{p}={_sha(c)}" for p, c in sorted(files.items())))
        if rest == f"/branches/{repo['default_branch']}":
            last = max(
                (p.get("merged_at") or p["updated_at"] for p in data["pull_requests"]),
                default=repo["created_at"],
            )
            commit = {"tree": {"sha": tree_sha}, "committer": {"date": last}}
            return httpx.Response(200, json={"commit": {"sha": tree_sha, "commit": commit}})
        if rest == "/commits":
            path = query.get("path", "")
            if path not in files:
                return httpx.Response(200, json=[])
            date = _file_date(files[path]) or repo["created_at"]
            return httpx.Response(200, json=[{"commit": {"committer": {"date": date}}}])
        if rest == f"/git/trees/{tree_sha}":
            tree = [{"path": p, "type": "blob", "sha": _sha(c)} for p, c in files.items()]
            return httpx.Response(200, json={"sha": tree_sha, "tree": tree})
        if c := re.fullmatch(r"/git/blobs/(\w+)", rest):
            for content in files.values():
                if _sha(content) == c[1]:
                    enc = base64.b64encode(content.encode()).decode()
                    return httpx.Response(200, json={"content": enc, "encoding": "base64"})
        return httpx.Response(404, json={"message": f"not in demo data: {request.url.path}"})

    def _paged(self, request: httpx.Request, items: list, query: dict) -> httpx.Response:
        size = min(int(query.get("per_page", "30")), PER_PAGE_MAX)
        page = int(query.get("page", "1"))
        headers = {}
        if page * size < len(items):
            nxt = request.url.copy_with(query=urlencode({**query, "page": str(page + 1)}).encode())
            headers["Link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, json=items[(page - 1) * size : page * size], headers=headers)


class DemoGitHubSource(GitHubSource):
    def __init__(self, repos: dict[str, dict[str, Any]], config: Config) -> None:
        # The demo history is from 2025; never cut it off by backfill_days.
        cfg = config.model_copy(
            update={"sources": config.sources.model_copy(update={"backfill_days": 36500})}
        )
        super().__init__("demo", cfg)
        self.gh = GitHub(
            "demo",
            base_url=cfg.sources.github_api_url,
            async_transport=DemoGitHubAPI(repos).transport(),
            http_cache=False,
        )


def scope_entries(repos: dict[str, dict[str, Any]]) -> list[ScopeEntry]:
    return [
        ScopeEntry(
            f"github:{full}",
            "github",
            full,
            full,
            {"private": bool(data["repository"].get("private")), "demo": True},
        )
        for full, data in sorted(repos.items())
    ]


async def load(store, *, data_dir: Path | None = None, extractor=None, progress=lambda _: None):
    """Scope the demo repos and sync them. Returns the SyncReport."""
    from .store import state as st
    from .sync import run_sync

    repos = load_data(data_dir)
    for entry in scope_entries(repos):
        await st.scope_add(store, entry)
    return await run_sync(
        store,
        {"github": DemoGitHubSource(repos, get_config())},
        extractor=extractor,
        only=[f"github:{full}" for full in repos],
        progress=progress,
    )
