"""Source-agnostic document shape that every connector normalizes into.

Connectors produce NormalizedDocument; the sync layer upserts them into source_document.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

import tiktoken

MAX_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class NormalizedDocument:
    # pull_request | pr_review | pr_review_comment | issue | issue_comment | markdown_file
    kind: str
    external_id: str  # stable id on the source (GitHub node_id; files: "{repo_id}:{path}")
    url: str  # exact citation link
    title: str | None
    body: str  # normalized markdown, already truncated to MAX_BODY_BYTES
    author_external_id: str | None
    author_login: str | None
    source_created_at: datetime | None
    source_updated_at: datetime | None
    parent_external_id: str | None = None  # comment/review -> its PR or issue
    metadata: dict[str, Any] = field(default_factory=dict)
    # Fields that define "the content changed". Volatile fields (reaction counts,
    # updated_at, comment counts) must NOT go in here.
    hash_fields: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> bytes:
        canonical = {"kind": self.kind, "title": self.title, "body": self.body, **self.hash_fields}
        data = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(data.encode()).digest()

    @property
    def token_count(self) -> int:
        return count_tokens(f"{self.title or ''}\n{self.body}")


def normalize_body(text: str | None) -> tuple[str, bool]:
    """CRLF -> LF, strip trailing whitespace per line, cap size. Returns (body, truncated)."""
    if not text:
        return "", False
    body = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    raw = body.encode()
    if len(raw) <= MAX_BODY_BYTES:
        return body, False
    return raw[:MAX_BODY_BYTES].decode(errors="ignore"), True


@lru_cache
def _encoding() -> tiktoken.Encoding:
    # Estimate only; matches OpenAI's text-embedding-3 tokenizer.
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text, disallowed_special=()))
