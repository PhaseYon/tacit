"""Split documents into retrieval chunks (langchain-text-splitters, token-sized)."""

from functools import lru_cache

from langchain_text_splitters import RecursiveCharacterTextSplitter

# Small enough for precise citations, big enough to keep an argument together.
CHUNK_TOKENS = 400
OVERLAP_TOKENS = 50


@lru_cache
def _splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name="cl100k_base",
        chunk_size=CHUNK_TOKENS,
        chunk_overlap=OVERLAP_TOKENS,
        # Markdown-aware separators first: headings, paragraphs, lines, sentences.
        separators=["\n## ", "\n### ", "\n\n", "\n", ". ", " ", ""],
    )


MIN_CHUNK_CHARS = 80


def chunk_text(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    pieces = [c.strip() for c in _splitter().split_text(text) if c.strip()]
    # Fold tiny pieces (usually a lone heading) into the next chunk.
    merged: list[str] = []
    carry = ""
    for piece in pieces:
        piece = f"{carry}\n\n{piece}" if carry else piece
        carry = ""
        if len(piece) < MIN_CHUNK_CHARS:
            carry = piece
        else:
            merged.append(piece)
    if carry:
        if merged:
            merged[-1] = f"{merged[-1]}\n\n{carry}"
        else:
            merged.append(carry)
    return merged


def context_header(kind: str, title: str | None, metadata: dict) -> str:
    """Short context prepended to every chunk before embedding, so a comment chunk
    still 'knows' which PR or issue it belongs to."""
    labels = {
        "pull_request": "Pull request",
        "issue": "Issue",
        "issue_comment": "Comment",
        "pr_review": "PR review",
        "pr_review_comment": "Code review comment",
        "markdown_file": "Doc",
        "slack_message": "Slack message",
        "slack_reply": "Slack thread reply",
    }
    parts = [labels.get(kind, kind)]
    number = metadata.get("number") or metadata.get("parent_number")
    if number:
        parts.append(f"#{number}")
    if metadata.get("channel"):
        parts.append(f"in {metadata['channel']}")
    if metadata.get("path"):
        parts.append(str(metadata["path"]))
    head = " ".join(parts)
    return f"{head}: {title}" if title else head
