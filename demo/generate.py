"""Build the demo dataset JSON from the hand-written story in demo/story/.

    python3 demo/generate.py

Writes demo/data/github/<repo>.json (GitHub REST API shapes) and
demo/benchmark/questions.jsonl. Pure stdlib and fully deterministic: same story in,
byte-identical files out. Ids, numbers and URLs are derived from the story order, so
edit the story and regenerate rather than editing the JSON by hand.
"""

import hashlib
import json
import re
import sys
import zlib
from datetime import datetime
from pathlib import Path

DEMO = Path(__file__).resolve().parent
sys.path.insert(0, str(DEMO))

from story import api, handbook, web  # noqa: E402
from story.cast import ORG, ORG_ID, PEOPLE, user  # noqa: E402
from story.model import PR, Issue, Repo  # noqa: E402
from story.questions import QUESTIONS  # noqa: E402

GH = "https://github.com"
API = "https://api.github.com"
REPOS: list[Repo] = [handbook.REPO, api.REPO, web.REPO]
QUESTION_TYPES = {"why", "who", "when", "what-changed", "evidence", "multi-hop"}
REF = re.compile(r"\{(url:)?([a-z]*)#([a-z0-9-]+)\}")


def ts(value: str) -> str:
    """'2025-03-10 09:12' -> '2025-03-10T09:12:00Z'."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M:%SZ")


def sha1(*parts: object) -> str:
    return hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()


def git_blob_sha(content: str) -> str:
    data = content.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def label(name: str) -> dict:
    lid = 5_000_000_000 + zlib.crc32(name.encode())
    color = format(zlib.crc32(name.encode()[::-1]) & 0xFFFFFF, "06x")
    return {"id": lid, "node_id": f"LA_kwDOLRK{lid}", "name": name, "color": color}


def association(login: str) -> str:
    return "MEMBER" if login in PEOPLE else "NONE"


class Builder:
    """Numbers every repo's issues/PRs, then resolves references and emits JSON."""

    def __init__(self, repos: list[Repo]):
        self.repos = repos
        self.urls: dict[str, str] = {}  # "repo:key" -> html_url
        self.numbers: dict[str, int] = {}  # "repo:key" -> number
        self.kinds: dict[str, str] = {}  # "repo:key" -> issue | pull
        self.visibility: dict[str, str] = {}  # repo name -> public | private
        for idx, repo in enumerate(repos, start=1):
            self.visibility[repo.name] = "private" if repo.private else "public"
            self._index(idx, repo)

    # --- pass 1: numbers, ids and URLs -----------------------------------------

    def _register(self, repo: Repo, key: str | None, url: str) -> None:
        if key is None:
            return
        full = f"{repo.name}:{key}"
        if full in self.urls:
            raise SystemExit(f"duplicate key {full}")
        self.urls[full] = url

    def _index(self, idx: int, repo: Repo) -> None:
        base = f"{GH}/{ORG}/{repo.name}"
        items = sorted(repo.items, key=lambda i: (i.at, i.key))
        repo._sorted = items  # type: ignore[attr-defined]
        repo._idx = idx  # type: ignore[attr-defined]
        events: list[tuple[str, int, str, object, object]] = []
        for number, item in enumerate(items, start=1):
            kind = "pull" if isinstance(item, PR) else "issues"
            item._number = number  # type: ignore[attr-defined]
            full = f"{repo.name}:{item.key}"
            self.numbers[full] = number
            self.kinds[full] = kind
            self._register(repo, item.key, f"{base}/{kind}/{number}")
            for j, cm in enumerate(item.comments):
                events.append((cm.at, 0, f"{item.key}/c{j}", item, cm))
            if isinstance(item, PR):
                for j, rv in enumerate(item.reviews):
                    events.append((rv.at, 1, f"{item.key}/r{j}", item, rv))
        # Comment/review ids grow with time, like on GitHub.
        seq = {"ic": 0, "rv": 0, "rc": 0}
        for _at, _o, _k, item, obj in sorted(events, key=lambda e: (e[0], e[1], e[2])):
            n = item._number  # type: ignore[attr-defined]
            kind = "pull" if isinstance(item, PR) else "issues"
            if hasattr(obj, "state"):  # review
                seq["rv"] += 1
                obj._id = 2_400_000_000 + idx * 1_000_000 + seq["rv"]
                self._register(repo, obj.key, f"{base}/pull/{n}#pullrequestreview-{obj._id}")
                for rcm in obj.comments:
                    seq["rc"] += 1
                    rcm._id = 1_700_000_000 + idx * 1_000_000 + seq["rc"]
                    self._register(repo, rcm.key, f"{base}/pull/{n}#discussion_r{rcm._id}")
            else:
                seq["ic"] += 1
                obj._id = 3_000_000_000 + idx * 1_000_000 + seq["ic"]
                self._register(repo, obj.key, f"{base}/{kind}/{n}#issuecomment-{obj._id}")
        for doc in repo.docs:
            self._register(repo, doc.key, f"{base}/blob/main/{doc.path}")

    # --- pass 2: text -----------------------------------------------------------

    def resolve(self, repo: Repo, text: str) -> str:
        def sub(m: re.Match) -> str:
            is_url, other, key = m.group(1), m.group(2), m.group(3)
            name = other or repo.name
            full = f"{name}:{key}"
            if full not in self.numbers:
                raise SystemExit(f"{repo.name}: unknown reference {m.group(0)}")
            if is_url:
                return self.urls[full]
            n = self.numbers[full]
            return f"#{n}" if name == repo.name and not other else f"{ORG}/{name}#{n}"

        return REF.sub(sub, text).strip("\n")

    # --- pass 3: JSON -----------------------------------------------------------

    def repo_json(self, repo: Repo) -> dict:
        idx = repo._idx  # type: ignore[attr-defined]
        rid = 710_000_000 + idx
        return {
            "id": rid,
            "node_id": f"R_kgDOLRK{rid}",
            "name": repo.name,
            "full_name": f"{ORG}/{repo.name}",
            "private": repo.private,
            "visibility": "private" if repo.private else "public",
            "owner": {"login": ORG, "id": ORG_ID, "type": "Organization"},
            "html_url": f"{GH}/{ORG}/{repo.name}",
            "description": repo.description,
            "url": f"{API}/repos/{ORG}/{repo.name}",
            "created_at": ts(repo.created),
            "default_branch": "main",
            "archived": False,
        }

    def build(self, repo: Repo) -> dict:
        full = f"{ORG}/{repo.name}"
        api_base = f"{API}/repos/{full}"
        out: dict = {
            "repository": self.repo_json(repo),
            "issues": [],
            "pull_requests": [],
            "pull_request_reviews": [],
            "issue_comments": [],
            "review_comments": [],
            "markdown_files": [],
        }
        idx = repo._idx  # type: ignore[attr-defined]
        for item in repo._sorted:  # type: ignore[attr-defined]
            n = item._number
            for login in [item.author, *item.assignees]:
                user(login)  # KeyError on unknown people
            activity = [item.at, *(cm.at for cm in item.comments)]
            if isinstance(item, PR):
                activity += [rv.at for rv in item.reviews]
                activity += [t for t in (item.merged, item.closed) if t]
                out["pull_requests"].append(self._pr(repo, item, max(activity)))
                rc_ids: dict[str, int] = {}
                for rv in item.reviews:
                    out["pull_request_reviews"].append(self._review(repo, item, rv))
                    for rcm in rv.comments:
                        if rcm.key:
                            rc_ids[rcm.key] = rcm._id
                        out["review_comments"].append(
                            self._review_comment(repo, item, rv, rcm, rc_ids)
                        )
            else:
                if item.closed:
                    activity.append(item.closed)
                out["issues"].append(self._issue(repo, item, max(activity), idx))
            kind = "pull" if isinstance(item, PR) else "issues"
            for cm in item.comments:
                user(cm.author)
                out["issue_comments"].append(
                    {
                        "url": f"{api_base}/issues/comments/{cm._id}",
                        "html_url": f"{GH}/{full}/{kind}/{n}#issuecomment-{cm._id}",
                        "issue_url": f"{api_base}/issues/{n}",
                        "id": cm._id,
                        "node_id": f"IC_kwDOLRK{cm._id}",
                        "user": user(cm.author),
                        "created_at": ts(cm.at),
                        "updated_at": ts(cm.at),
                        "author_association": association(cm.author),
                        "body": self.resolve(repo, cm.body),
                        "reactions": {"total_count": 0},
                    }
                )
        out["issue_comments"].sort(key=lambda c: c["id"])
        out["pull_request_reviews"].sort(key=lambda r: r["id"])
        out["review_comments"].sort(key=lambda r: r["id"])
        for doc in sorted(repo.docs, key=lambda d: d.path):
            content = self.resolve(repo, doc.content) + "\n"
            out["markdown_files"].append(
                {"path": doc.path, "sha": git_blob_sha(content), "content": content}
            )
        return out

    def _issue(self, repo: Repo, it: Issue, updated: str, idx: int) -> dict:
        full = f"{ORG}/{repo.name}"
        n = it._number  # type: ignore[attr-defined]
        iid = 2_100_000_000 + idx * 100_000 + n
        if it.closed and it.state_reason not in ("completed", "not_planned"):
            raise SystemExit(f"{repo.name}:{it.key} closed without state_reason")
        return {
            "url": f"{API}/repos/{full}/issues/{n}",
            "repository_url": f"{API}/repos/{full}",
            "comments_url": f"{API}/repos/{full}/issues/{n}/comments",
            "html_url": f"{GH}/{full}/issues/{n}",
            "id": iid,
            "node_id": f"I_kwDOLRK{idx}{n:04d}",
            "number": n,
            "title": it.title,
            "user": user(it.author),
            "labels": [label(lb) for lb in it.labels],
            "state": "closed" if it.closed else "open",
            "state_reason": it.state_reason if it.closed else None,
            "locked": False,
            "assignees": [user(a) for a in it.assignees],
            "comments": len(it.comments),
            "created_at": ts(it.at),
            "updated_at": ts(updated),
            "closed_at": ts(it.closed) if it.closed else None,
            "author_association": association(it.author),
            "body": self.resolve(repo, it.body),
            "reactions": {"total_count": 0},
        }

    def _pr(self, repo: Repo, pr: PR, updated: str) -> dict:
        full = f"{ORG}/{repo.name}"
        idx = repo._idx  # type: ignore[attr-defined]
        n = pr._number  # type: ignore[attr-defined]
        pid = 1_900_000_000 + idx * 100_000 + n
        if pr.merged and pr.closed:
            raise SystemExit(f"{repo.name}:{pr.key} both merged and closed")
        closed_at = pr.merged or pr.closed
        branch = pr.branch or pr.key
        return {
            "url": f"{API}/repos/{full}/pulls/{n}",
            "id": pid,
            "node_id": f"PR_kwDOLRK{idx}{n:04d}",
            "html_url": f"{GH}/{full}/pull/{n}",
            "issue_url": f"{API}/repos/{full}/issues/{n}",
            "number": n,
            "state": "closed" if closed_at else "open",
            "locked": False,
            "title": pr.title,
            "user": user(pr.author),
            "body": self.resolve(repo, pr.body),
            "labels": [label(lb) for lb in pr.labels],
            "assignees": [user(a) for a in pr.assignees],
            "requested_reviewers": [],
            "created_at": ts(pr.at),
            "updated_at": ts(updated),
            "closed_at": ts(closed_at) if closed_at else None,
            "merged_at": ts(pr.merged) if pr.merged else None,
            "merge_commit_sha": sha1(full, pr.key, "merge") if pr.merged else None,
            "head": {
                "label": f"{ORG}:{branch}",
                "ref": branch,
                "sha": sha1(full, pr.key, "head"),
            },
            "base": {"label": f"{ORG}:main", "ref": "main", "sha": sha1(full, pr.key, "base")},
            "author_association": association(pr.author),
            "draft": pr.draft,
            "merged": bool(pr.merged),
            "merged_by": user(pr.author) if pr.merged else None,
            "comments": len(pr.comments),
            "review_comments": sum(len(r.comments) for r in pr.reviews),
        }

    def _review(self, repo: Repo, pr: PR, rv) -> dict:
        full = f"{ORG}/{repo.name}"
        n = pr._number  # type: ignore[attr-defined]
        user(rv.author)
        if rv.state not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"):
            raise SystemExit(f"{repo.name}:{pr.key} bad review state {rv.state}")
        return {
            "id": rv._id,
            "node_id": f"PRR_kwDOLRK{rv._id}",
            "user": user(rv.author),
            "body": self.resolve(repo, rv.body) if rv.body else "",
            "state": rv.state,
            "html_url": f"{GH}/{full}/pull/{n}#pullrequestreview-{rv._id}",
            "pull_request_url": f"{API}/repos/{full}/pulls/{n}",
            "author_association": association(rv.author),
            "submitted_at": ts(rv.at),
            "commit_id": sha1(full, pr.key, "head"),
        }

    def _review_comment(self, repo: Repo, pr: PR, rv, rcm, rc_ids: dict[str, int]) -> dict:
        full = f"{ORG}/{repo.name}"
        n = pr._number  # type: ignore[attr-defined]
        reply = None
        if rcm.reply_to:
            if rcm.reply_to not in rc_ids:
                raise SystemExit(f"{repo.name}:{pr.key} reply_to unknown {rcm.reply_to}")
            reply = rc_ids[rcm.reply_to]
        hunk = rcm.hunk or f"@@ -{max(rcm.line - 3, 1)},6 +{max(rcm.line - 3, 1)},7 @@"
        return {
            "url": f"{API}/repos/{full}/pulls/comments/{rcm._id}",
            "pull_request_review_id": rv._id,
            "id": rcm._id,
            "node_id": f"PRRC_kwDOLRK{rcm._id}",
            "diff_hunk": hunk,
            "path": rcm.path,
            "commit_id": sha1(full, pr.key, "head"),
            "original_commit_id": sha1(full, pr.key, "head"),
            "user": user(rv.author),
            "body": self.resolve(repo, rcm.body),
            "created_at": ts(rv.at),
            "updated_at": ts(rv.at),
            "html_url": f"{GH}/{full}/pull/{n}#discussion_r{rcm._id}",
            "pull_request_url": f"{API}/repos/{full}/pulls/{n}",
            "author_association": association(rv.author),
            "in_reply_to_id": reply,
            "start_line": None,
            "original_start_line": None,
            "line": rcm.line,
            "original_line": rcm.line,
            "side": "RIGHT",
        }

    def questions(self) -> list[dict]:
        out = []
        seen: set[str] = set()
        for q in QUESTIONS:
            if q["id"] in seen:
                raise SystemExit(f"duplicate question id {q['id']}")
            seen.add(q["id"])
            if q["type"] not in QUESTION_TYPES:
                raise SystemExit(f"{q['id']}: bad type {q['type']}")
            urls = []
            for ref in q["gold"]:
                if ref not in self.urls:
                    raise SystemExit(f"{q['id']}: unknown gold source {ref}")
                urls.append(self.urls[ref])
            repos = {ref.split(":", 1)[0] for ref in q["gold"]}
            access = "public" if all(self.visibility[r] == "public" for r in repos) else "private"
            out.append(
                {
                    "id": q["id"],
                    "question": q["question"],
                    "type": q["type"],
                    "gold_answer": q["answer"],
                    "gold_sources": urls,
                    "requires_history": q.get("history", False),
                    "access": access,
                }
            )
        return out


def main() -> None:
    b = Builder(REPOS)
    out_dir = DEMO / "data" / "github"
    out_dir.mkdir(parents=True, exist_ok=True)
    for repo in REPOS:
        data = b.build(repo)
        path = out_dir / f"{repo.name}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(
            f"{path.relative_to(DEMO)}: {len(data['pull_requests'])} PRs,"
            f" {len(data['issues'])} issues, {len(data['issue_comments'])} comments,"
            f" {len(data['pull_request_reviews'])} reviews,"
            f" {len(data['review_comments'])} review comments,"
            f" {len(data['markdown_files'])} docs"
        )
    qs = b.questions()
    qpath = DEMO / "benchmark" / "questions.jsonl"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in qs))
    print(f"{qpath.relative_to(DEMO)}: {len(qs)} questions")


if __name__ == "__main__":
    main()
