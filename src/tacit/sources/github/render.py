"""Render a GitHub item (PR/issue + its discussion, or a markdown doc) as one transcript.

The transcript is what gets chunked for search and, for items worth it, extracted into
the graph. Speakers are always `@login` so people resolve deterministically across
items. State, labels and approvals are part of the text, so a change to any of them
changes the content hash and re-indexes the item.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..documents import count_tokens

MAX_EPISODE_TOKENS = 12_000
KEEP_FIRST, KEEP_LAST = 5, 15

ROLE = {
    "pull_request": "PR author",
    "issue": "issue author",
    "issue_comment": "comment",
    "pr_review": "review",
    "pr_review_comment": "code review comment",
    "markdown_file": "doc",
}


@dataclass(frozen=True)
class Member:
    """One speaker turn: the root post, a comment, a review or an inline comment."""

    kind: str
    author_login: str | None
    created_at: datetime | None
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Bundle:
    """A root document plus its human comments/reviews, in time order."""

    repo: str  # "owner/repo"
    kind: str  # pull_request | issue | markdown_file
    title: str | None
    root_metadata: dict[str, Any]
    members: list[Member] = field(default_factory=list)

    @property
    def participants(self) -> list[str]:
        """Lowercased logins of everyone who spoke or approved, sorted."""
        logins = {m.author_login for m in self.members if m.author_login}
        logins |= set(self.root_metadata.get("approved_by") or [])
        return sorted({login.lower() for login in logins})

    @property
    def state(self) -> str | None:
        m = self.root_metadata
        if m.get("merged_at"):
            return "merged"
        return m.get("state")


def render(bundle: Bundle) -> str:
    """Transcript-style text, capped at MAX_EPISODE_TOKENS by dropping middle turns."""
    m = bundle.root_metadata
    header = [f"Repository: {bundle.repo}"]
    if bundle.kind == "markdown_file":
        header.append(f"Document: {m.get('path')} — {bundle.title}")
        body = "\n\n".join(x.body.strip() for x in bundle.members if x.body.strip())
        return "\n".join(header) + ("\n\n" + body if body else "")

    kind = "Pull request" if bundle.kind == "pull_request" else "Issue"
    header.append(f"{kind} #{m.get('number')}: {bundle.title}")
    if m.get("labels"):
        header.append(f"Labels: {', '.join(m['labels'])}")
    state = bundle.state or "unknown"
    if state == "open" and m.get("draft"):
        state = "open (draft)"
    header.append(f"State: {state}")
    if m.get("approved_by"):
        header.append("Approved by: " + ", ".join(f"@{a}" for a in m["approved_by"]))

    lines = [_line(x) for x in bundle.members]
    text = "\n".join(header) + "\n\n" + "\n\n".join(lines)
    if count_tokens(text) <= MAX_EPISODE_TOKENS or len(lines) <= KEEP_FIRST + KEEP_LAST + 1:
        return text
    omitted = len(lines) - (KEEP_FIRST + KEEP_LAST + 1)
    kept = lines[: KEEP_FIRST + 1] + [f"[{omitted} comments omitted]"] + lines[-KEEP_LAST:]
    return "\n".join(header) + "\n\n" + "\n\n".join(kept)


def _line(mem: Member) -> str:
    who = f"@{mem.author_login}" if mem.author_login else "someone"
    role = ROLE.get(mem.kind, mem.kind)
    if mem.kind == "pr_review" and mem.metadata.get("state"):
        role = f"review, {str(mem.metadata['state']).lower()}"
    if mem.kind == "pr_review_comment" and mem.metadata.get("path"):
        role = f"comment on {mem.metadata['path']}"
    date = mem.created_at.date().isoformat() if mem.created_at else "undated"
    body = mem.body.strip() or "(no text)"
    return f"{who} ({role}, {date}): {body}"
