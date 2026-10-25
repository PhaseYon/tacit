"""Referential-integrity checks for the demo dataset. Stdlib only.

    python3 demo/validate.py            # from the repo root
    uv run python ../../demo/validate.py  # from apps/api

Checks that comments and reviews point at existing issues/PRs, review replies and review
ids resolve, ids are unique, every person is in the cast, approvers are people, superseded
ADRs and their successors link both ways, dates fall in the story window, and every
benchmark gold source is a document in the data. Exits non-zero on any problem.
"""

import json
import re
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parent
sys.path.insert(0, str(DEMO))

from story.cast import BOTS, PEOPLE  # noqa: E402

WINDOW = ("2025-03-01T00:00:00Z", "2026-10-01T00:00:00Z")
ADR_FILE = re.compile(r"docs/adr/(\d{4})-[^/]+\.md$")
SUPERSEDED_BY = re.compile(r"^- Status: Superseded by \[ADR-(\d{4})\]", re.M)
SUPERSEDES = re.compile(r"^- Supersedes: \[ADR-(\d{4})\]", re.M)


def number(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def check_repo(name: str, data: dict, errors: list[str], urls: set[str]) -> None:
    def err(msg: str) -> None:
        errors.append(f"{name}: {msg}")

    repo = data["repository"]
    issue_numbers = {i["number"] for i in data["issues"]}
    pr_numbers = {p["number"] for p in data["pull_requests"]}
    if issue_numbers & pr_numbers:
        err(f"issue and PR numbers overlap: {sorted(issue_numbers & pr_numbers)}")
    review_ids = {r["id"] for r in data["pull_request_reviews"]}
    rc_ids = {c["id"] for c in data["review_comments"]}

    # Unique ids and node_ids across every object in the repo.
    seen: dict[str, str] = {}
    for key in (
        "issues",
        "pull_requests",
        "pull_request_reviews",
        "issue_comments",
        "review_comments",
    ):
        for obj in data[key]:
            for field in ("id", "node_id", "html_url"):
                tag = f"{field}={obj[field]}"
                if tag in seen:
                    err(f"duplicate {tag} in {key} and {seen[tag]}")
                seen[tag] = key
            urls.add(obj["html_url"])
            login = (obj.get("user") or {}).get("login")
            if login not in PEOPLE and login not in BOTS:
                err(f"{obj['html_url']}: author {login!r} is not in the cast")
            for ts_field in ("created_at", "submitted_at"):
                if (t := obj.get(ts_field)) and not (WINDOW[0] <= t < WINDOW[1]):
                    err(f"{obj['html_url']}: {ts_field} {t} outside {WINDOW}")

    for c in data["issue_comments"]:
        if number(c["issue_url"]) not in issue_numbers | pr_numbers:
            err(f"comment {c['id']} points at missing #{number(c['issue_url'])}")
    for r in data["pull_request_reviews"]:
        if number(r["pull_request_url"]) not in pr_numbers:
            err(f"review {r['id']} points at missing PR #{number(r['pull_request_url'])}")
    for c in data["review_comments"]:
        if number(c["pull_request_url"]) not in pr_numbers:
            err(f"review comment {c['id']} points at missing PR")
        if c["pull_request_review_id"] not in review_ids:
            err(f"review comment {c['id']} has unknown review id")
        if c["in_reply_to_id"] is not None and c["in_reply_to_id"] not in rc_ids:
            err(f"review comment {c['id']} replies to unknown comment")

    for p in data["pull_requests"]:
        if p["merged"] != bool(p["merged_at"]):
            err(f"PR #{p['number']} merged flag disagrees with merged_at")
        for lb in p["labels"]:
            if not lb.get("name"):
                err(f"PR #{p['number']} has a label without a name")
    for i in data["issues"]:
        if "pull_request" in i:
            err(f"issue #{i['number']} carries a pull_request key")
        if (i["state"] == "closed") != bool(i["closed_at"]):
            err(f"issue #{i['number']} state disagrees with closed_at")

    # Approvers (latest decisive review APPROVED) must be people in the cast.
    for r in data["pull_request_reviews"]:
        if r["state"] == "APPROVED" and r["user"]["login"] not in PEOPLE:
            err(f"approver {r['user']['login']} on {r['html_url']} is not a person in the cast")

    adrs: dict[str, str] = {}
    for f in data["markdown_files"]:
        urls.add(f"{repo['html_url']}/blob/{repo['default_branch']}/{f['path']}")
        if m := ADR_FILE.search(f["path"]):
            adrs[m.group(1)] = f["content"]
    for num, content in adrs.items():
        if m := SUPERSEDED_BY.search(content):
            succ = m.group(1)
            if succ not in adrs:
                err(f"ADR-{num} is superseded by missing ADR-{succ}")
            elif f"[ADR-{num}]" not in (SUPERSEDES.search(adrs[succ]) or [""])[0]:
                err(f"ADR-{succ} does not link back to ADR-{num} with 'Supersedes:'")
        if m := SUPERSEDES.search(content):
            old = m.group(1)
            if (
                old not in adrs
                or f"[ADR-{num}]" not in (SUPERSEDED_BY.search(adrs[old]) or [""])[0]
            ):
                err(f"ADR-{num} supersedes ADR-{old}, but ADR-{old} isn't marked superseded by it")


def main() -> int:
    errors: list[str] = []
    urls: set[str] = set()
    data_dir = DEMO / "data" / "github"
    files = sorted(data_dir.glob("*.json"))
    if not files:
        print(f"no data in {data_dir}")
        return 1
    for path in files:
        check_repo(path.stem, json.loads(path.read_text()), errors, urls)

    qpath = DEMO / "benchmark" / "questions.jsonl"
    questions = [json.loads(line) for line in qpath.read_text().splitlines() if line.strip()]
    required = {
        "id",
        "question",
        "type",
        "gold_answer",
        "gold_sources",
        "requires_history",
        "access",
    }
    for q in questions:
        if missing := required - q.keys():
            errors.append(f"question {q.get('id')}: missing fields {sorted(missing)}")
        for u in q.get("gold_sources", []):
            if u not in urls:
                errors.append(f"question {q['id']}: gold source not in data: {u}")
        if not q.get("gold_sources"):
            errors.append(f"question {q['id']}: no gold sources")

    for e in errors:
        print(f"ERROR {e}")
    print(f"{len(files)} repos, {len(questions)} questions, {len(errors)} problems")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
