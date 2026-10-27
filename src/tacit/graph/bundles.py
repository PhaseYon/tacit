"""Which documents become graph episodes, and how they're rendered (design §4).

A bundle is a root document (merged PR, closed+discussed issue, markdown doc) plus its
human comments/reviews. Speakers are always rendered as @login so people resolve
deterministically (design §6).
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text

from ..connectors.documents import count_tokens
from ..db import worker_session
from .ontology import ONTOLOGY_VERSION

RENDERER_VERSION = "1"
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
    document_id: str
    kind: str
    author_login: str | None
    created_at: datetime | None
    body: str
    metadata: dict[str, Any]
    content_hash: bytes


@dataclass
class Bundle:
    root_id: str
    container_id: str
    container_name: str
    kind: str
    title: str | None
    url: str
    reference_time: datetime | None
    root_metadata: dict[str, Any]
    members: list[Member] = field(default_factory=list)

    @property
    def participants(self) -> set[str]:
        logins = {m.author_login for m in self.members if m.author_login}
        logins |= set(self.root_metadata.get("approved_by") or [])
        return {login.lower() for login in logins}

    def input_hash(self, model_ids: str, embedding_model: str) -> bytes:
        payload = json.dumps(
            {
                "renderer": RENDERER_VERSION,
                "ontology": ONTOLOGY_VERSION,
                "models": model_ids,
                "embedding": embedding_model,
                "members": sorted((m.document_id, m.content_hash.hex()) for m in self.members),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).digest()

    @property
    def name(self) -> str:
        if self.kind == "markdown_file":
            return f"{self.container_name}:{self.root_metadata.get('path')}"
        return f"{self.container_name}#{self.root_metadata.get('number')}: {self.title}"


def _meta(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else json.loads(v or "{}")


def render(bundle: Bundle) -> str:
    """Transcript-style episode body, capped at MAX_EPISODE_TOKENS."""
    m = bundle.root_metadata
    header = [f"Repository: {bundle.container_name}"]
    if bundle.kind == "markdown_file":
        header.append(f"Document: {m.get('path')} — {bundle.title}")
    else:
        kind = "Pull request" if bundle.kind == "pull_request" else "Issue"
        header.append(f"{kind} #{m.get('number')}: {bundle.title}")
        if m.get("labels"):
            header.append(f"Labels: {', '.join(m['labels'])}")
        state = "merged" if m.get("merged_at") else m.get("state")
        header.append(f"State: {state}")
        if m.get("approved_by"):
            header.append("Approved by: " + ", ".join(f"@{a}" for a in m["approved_by"]))

    def line(mem: Member) -> str:
        who = f"@{mem.author_login}" if mem.author_login else "someone"
        role = ROLE.get(mem.kind, mem.kind)
        if mem.kind == "pr_review" and mem.metadata.get("state"):
            role = f"review, {str(mem.metadata['state']).lower()}"
        if mem.kind == "pr_review_comment" and mem.metadata.get("path"):
            role = f"comment on {mem.metadata['path']}"
        date = mem.created_at.date().isoformat() if mem.created_at else "undated"
        return f"{who} ({role}, {date}): {mem.body.strip()}"

    lines = [line(x) for x in bundle.members]
    body = "\n".join(header) + "\n\n" + "\n\n".join(lines)
    if count_tokens(body) <= MAX_EPISODE_TOKENS or len(lines) <= KEEP_FIRST + KEEP_LAST + 1:
        return body
    omitted = len(lines) - (KEEP_FIRST + KEEP_LAST + 1)
    kept = lines[: KEEP_FIRST + 1] + [f"[{omitted} comments omitted]"] + lines[-KEEP_LAST:]
    return "\n".join(header) + "\n\n" + "\n\n".join(kept)


ELIGIBLE_ROOTS = """
SELECT d.id::text, d.container_id::text, c.name, d.kind, d.title, d.url, d.metadata,
       d.source_created_at, d.source_updated_at, d.author_login, d.body, d.content_hash
FROM source_document d JOIN source_container c ON c.id = d.container_id
WHERE d.deleted_at IS NULL AND c.is_active
  AND (:include_private OR c.visibility = 'public')
  AND (
    (d.kind = 'pull_request' AND d.metadata->>'merged_at' IS NOT NULL)
    OR d.kind = 'markdown_file'
    OR (d.kind = 'issue' AND d.metadata->>'state' = 'closed' AND (
          SELECT count(*) FROM source_document k
          WHERE k.parent_id = d.id AND k.kind = 'issue_comment' AND k.deleted_at IS NULL) >= 2)
  )
"""

CHILDREN = """
SELECT parent_id::text, id::text, kind, author_login, source_created_at, body, metadata,
       content_hash
FROM source_document
WHERE parent_id = ANY(CAST(:ids AS uuid[])) AND deleted_at IS NULL
ORDER BY source_created_at NULLS FIRST
"""


async def eligible_bundles(workspace_id: str, *, include_private: bool) -> list[Bundle]:
    async with worker_session(workspace_id) as s:
        roots = (await s.execute(text(ELIGIBLE_ROOTS), {"include_private": include_private})).all()
        bundles: dict[str, Bundle] = {}
        for r in roots:
            meta = _meta(r.metadata)
            if r.kind == "pull_request":
                ref = meta.get("merged_at")
                ref_time = datetime.fromisoformat(ref) if ref else r.source_updated_at
            else:
                ref_time = r.source_updated_at or r.source_created_at
            b = Bundle(r[0], r[1], r.name, r.kind, r.title, r.url, ref_time, meta)
            b.members.append(
                Member(
                    r[0],
                    r.kind,
                    r.author_login,
                    r.source_created_at,
                    r.body,
                    meta,
                    bytes(r.content_hash),
                )
            )
            bundles[b.root_id] = b
        if bundles:
            kids = await s.execute(text(CHILDREN), {"ids": list(bundles)})
            for k in kids:
                bundles[k[0]].members.append(
                    Member(
                        k[1],
                        k.kind,
                        k.author_login,
                        k.source_created_at,
                        k.body,
                        _meta(k.metadata),
                        bytes(k.content_hash),
                    )
                )
    return sorted(bundles.values(), key=lambda b: (b.reference_time is None, b.reference_time))
