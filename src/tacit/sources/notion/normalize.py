"""Pure functions: Notion page + block tree -> NormalizedDocument, and page-tree roots.

No I/O. Blocks are API block objects; the fetcher attaches each block's children (when
`has_children`) under the CHILDREN key, so rendering is a plain recursive walk.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..documents import NormalizedDocument, normalize_body

KIND = "notion_page"
CHILDREN = "_children"
TITLE_MAX = 200
# Property types whose values are stable, human-written facts. Formulas, rollups,
# relations and created/edited stamps change without anyone editing the page.
HEADER_PROPERTIES = {
    "select",
    "status",
    "multi_select",
    "date",
    "people",
    "rich_text",
    "number",
    "checkbox",
    "url",
    "email",
    "phone_number",
    "unique_id",
}


@dataclass(frozen=True)
class Context:
    connection_id: str
    notion_workspace_id: str
    users: dict[str, str] = field(default_factory=dict)  # Notion user id -> name

    def external_id(self, page_id: str) -> str:
        return f"{self.connection_id}:{page_id}"

    def person(self, user_id: str | None) -> str | None:
        return f"{self.notion_workspace_id}:{user_id}" if user_id else None


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def is_trashed(obj: dict[str, Any]) -> bool:
    return bool(obj.get("in_trash") or obj.get("archived") or obj.get("is_archived"))


# --- Rich text and titles -------------------------------------------------------------


def rich_text(items: Iterable[dict[str, Any]] | None) -> str:
    out = []
    for item in items or []:
        text = item.get("plain_text") or ""
        href = item.get("href")
        if href and href != text and item.get("type") == "text":
            text = f"{text} ({href})"
        out.append(text)
    return "".join(out)


def title_of(obj: dict[str, Any]) -> str:
    """Page title (the `title`-typed property) or a data source's title."""
    if isinstance(obj.get("title"), list):
        return rich_text(obj["title"]).strip()
    for prop in (obj.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return rich_text(prop.get("title")).strip()
    return ""


def _property_value(prop: dict[str, Any], users: dict[str, str]) -> str:
    t = prop.get("type")
    v = prop.get(t) if t else None
    if v is None or t not in HEADER_PROPERTIES:
        return ""
    if t in ("select", "status"):
        return v.get("name") or ""
    if t == "multi_select":
        return ", ".join(o.get("name", "") for o in v)
    if t == "date":
        return v.get("start", "") + (f" → {v['end']}" if v.get("end") else "")
    if t == "people":
        return ", ".join(p.get("name") or users.get(p.get("id", ""), "") for p in v)
    if t == "rich_text":
        return rich_text(v)
    if t == "checkbox":
        return "Yes" if v else "No"
    if t == "unique_id":
        return f"{v.get('prefix') + '-' if v.get('prefix') else ''}{v.get('number')}"
    return str(v)


def properties_header(page: dict[str, Any], users: dict[str, str]) -> list[str]:
    lines = []
    for name, prop in sorted((page.get("properties") or {}).items()):
        if isinstance(prop, dict) and (value := _property_value(prop, users).strip()):
            lines.append(f"{name}: {value}")
    return lines


# --- Blocks ---------------------------------------------------------------------------


def _text(block: dict[str, Any]) -> str:
    return rich_text((block.get(block.get("type", "")) or {}).get("rich_text"))


def _file_line(block: dict[str, Any], label: str) -> str:
    data = block.get(block["type"]) or {}
    name = data.get("name") or ""
    caption = rich_text(data.get("caption"))
    return " ".join(x for x in [f"[{label}{': ' + name if name else ''}]", caption] if x)


def render_blocks(blocks: list[dict[str, Any]], depth: int = 0) -> list[str]:
    """Blocks -> markdown-ish lines. Child pages are separate documents (title only)."""
    lines: list[str] = []
    pad = "  " * depth
    number = 0
    for b in blocks:
        t = b.get("type")
        kids = b.get(CHILDREN) or []
        number = number + 1 if t == "numbered_list_item" else 0
        nested_depth = depth + 1
        line: str | None
        if t == "paragraph":
            line = _text(b)
        elif t in ("heading_1", "heading_2", "heading_3"):
            line = "#" * int(t[-1]) + " " + _text(b)
            nested_depth = depth  # toggleable headings: children read as the section
        elif t == "bulleted_list_item":
            line = "- " + _text(b)
        elif t == "numbered_list_item":
            line = f"{number}. " + _text(b)
        elif t == "to_do":
            done = (b.get("to_do") or {}).get("checked")
            line = f"- [{'x' if done else ' '}] " + _text(b)
        elif t == "toggle":
            line = _text(b)
        elif t == "quote":
            line = "> " + _text(b)
        elif t == "callout":
            icon = ((b.get("callout") or {}).get("icon") or {}).get("emoji") or ""
            line = "> " + (f"{icon} " if icon else "") + _text(b)
        elif t == "code":
            lang = (b.get("code") or {}).get("language") or ""
            body = _text(b)
            lines.extend(f"{pad}{x}" for x in [f"```{lang}", *body.split("\n"), "```"])
            line = None
        elif t == "equation":
            line = "$$" + ((b.get("equation") or {}).get("expression") or "") + "$$"
        elif t == "divider":
            line = "---"
        elif t == "table":
            line = None
            for row in kids:
                cells = (row.get("table_row") or {}).get("cells") or []
                lines.append(pad + " | ".join(rich_text(c) for c in cells))
            kids = []
        elif t in ("bookmark", "embed", "link_preview"):
            data = b.get(t) or {}
            line = " ".join(x for x in [data.get("url") or "", rich_text(data.get("caption"))] if x)
        elif t in ("image", "file", "pdf", "video", "audio"):
            line = _file_line(b, t)
        elif t == "child_page":
            line = f"[page: {(b.get('child_page') or {}).get('title', '')}]"
            kids = []
        elif t == "child_database":
            line = f"[database: {(b.get('child_database') or {}).get('title', '')}]"
            kids = []
        elif t in ("synced_block", "column_list", "column"):
            line = None
            nested_depth = depth  # layout only: children render inline
        else:
            # table_of_contents, breadcrumb, link_to_page, unsupported, ...: no text
            line = None
            kids = []
        if line is not None and line.strip():
            lines.append(pad + line)
        if kids:
            lines.extend(render_blocks(kids, nested_depth))
    return lines


# --- Documents ------------------------------------------------------------------------


def normalize_page(
    page: dict[str, Any], blocks: list[dict[str, Any]], ctx: Context
) -> NormalizedDocument | None:
    """A Notion page -> one document. None for trashed pages (callers tombstone)."""
    if page.get("object") != "page" or is_trashed(page):
        return None
    title = title_of(page)[:TITLE_MAX] or "Untitled"
    parent_type, parent_id = parent_ref(page)
    header = properties_header(page, ctx.users) if parent_type in DATABASE_PARENTS else []
    text = "\n".join(header + ([""] if header else []) + render_blocks(blocks))
    body, truncated = normalize_body(text)
    author = (page.get("created_by") or {}).get("id")
    return NormalizedDocument(
        kind=KIND,
        external_id=ctx.external_id(page["id"]),
        url=page.get("url") or f"https://www.notion.so/{page['id'].replace('-', '')}",
        title=title,
        body=body,
        author_external_id=ctx.person(author),
        author_login=ctx.users.get(author or ""),
        source_created_at=parse_time(page.get("created_time")),
        source_updated_at=parse_time(page.get("last_edited_time")),
        # last_edited_time, parent and the tree live in metadata, outside the hash.
        metadata={
            "page_id": page["id"],
            "last_edited_time": page.get("last_edited_time"),
            "parent_type": parent_type,
            "truncated": truncated,
        },
    )


# --- Trees ----------------------------------------------------------------------------

DATABASE_PARENTS = {"data_source", "database"}


def parent_ref(obj: dict[str, Any]) -> tuple[str, str | None]:
    """('page'|'data_source'|'database'|'block'|'workspace', id). A data source's own
    parent is its database; we skip to the database's parent when Notion gives it."""
    parent = obj.get("parent") or {}
    if obj.get("object") == "data_source" and obj.get("database_parent"):
        parent = obj["database_parent"]
    t = parent.get("type") or "workspace"
    short = t.removesuffix("_id")
    return short, parent.get(t) if t != "workspace" else None


@dataclass(frozen=True)
class Node:
    id: str
    object: str  # page | data_source
    parent_type: str
    parent_id: str | None
    database_id: str | None = None


def compute_roots(nodes: Iterable[Node]) -> dict[str, str]:
    """node id -> its tree root id. Walks parents through nodes this connection can see;
    anything unresolvable (workspace, block, not shared) ends the walk, so an unknown
    parent makes a page its own (private) tree. Loops end at the node itself."""
    by_id = {n.id: n for n in nodes}
    by_database = {n.database_id: n.id for n in by_id.values() if n.database_id}

    def parent(n: Node) -> str | None:
        if n.parent_type in ("page", "data_source") and n.parent_id in by_id:
            return n.parent_id
        if n.parent_type == "database" and n.parent_id in by_database:
            pid = by_database[n.parent_id]
            return pid if pid != n.id else None
        return None

    roots: dict[str, str] = {}
    for start in by_id.values():
        seen, cur = {start.id}, start
        while (pid := parent(cur)) is not None:
            if pid in seen:  # a parent loop: keep the page in its own tree
                cur = start
                break
            seen.add(pid)
            cur = by_id[pid]
        roots[start.id] = cur.id
    return roots
