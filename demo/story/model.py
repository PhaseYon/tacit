"""A tiny DSL for writing the demo story by hand.

Story files build lists of `Issue`, `PR` and `Doc` objects with symbolic keys; generate.py
turns them into GitHub REST-shaped JSON with stable ids, numbers and URLs.

Text may reference other items by key, resolved after numbering:
  {#key}        -> "#12"                 (same repo)
  {api#key}     -> "larkspur/api#12"     (other repo)
  {url:api#key} -> "https://github.com/larkspur/api/pull/12"
"""

from dataclasses import dataclass, field


@dataclass
class Comment:
    """Conversation comment on an issue or PR (GitHub "issue comment")."""

    author: str
    at: str
    body: str
    key: str | None = None


@dataclass
class ReviewComment:
    """Inline diff comment. `reply_to` is the key of an earlier review comment."""

    path: str
    line: int
    body: str
    hunk: str = ""
    key: str | None = None
    reply_to: str | None = None


@dataclass
class Review:
    author: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED
    at: str
    body: str = ""
    comments: list[ReviewComment] = field(default_factory=list)
    key: str | None = None


@dataclass
class Issue:
    key: str
    title: str
    author: str
    at: str
    body: str
    closed: str | None = None
    state_reason: str | None = None  # completed | not_planned (when closed)
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)


@dataclass
class PR:
    key: str
    title: str
    author: str
    at: str
    body: str
    merged: str | None = None
    closed: str | None = None  # closed without merging
    labels: list[str] = field(default_factory=list)
    branch: str | None = None
    draft: bool = False
    reviews: list[Review] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)


@dataclass
class Doc:
    """A markdown file as it exists on the default branch today."""

    path: str
    content: str
    key: str | None = None


@dataclass
class Repo:
    name: str
    private: bool
    description: str
    created: str
    items: list[Issue | PR]
    docs: list[Doc]


# Short constructors keep the story files readable.
def c(author: str, at: str, body: str, key: str | None = None) -> Comment:
    return Comment(author, at, body, key)


def approve(author: str, at: str, body: str = "", key: str | None = None, comments=None) -> Review:
    return Review(author, "APPROVED", at, body, comments or [], key)


def comment_review(
    author: str, at: str, body: str = "", key: str | None = None, comments=None
) -> Review:
    return Review(author, "COMMENTED", at, body, comments or [], key)


def request_changes(
    author: str, at: str, body: str = "", key: str | None = None, comments=None
) -> Review:
    return Review(author, "CHANGES_REQUESTED", at, body, comments or [], key)


def rc(
    path: str,
    line: int,
    body: str,
    hunk: str = "",
    key: str | None = None,
    reply_to: str | None = None,
) -> ReviewComment:
    return ReviewComment(path, line, body, hunk, key, reply_to)
