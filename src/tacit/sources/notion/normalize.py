"""Pure functions: Notion page + block tree -> one Item, plus id and parent helpers.

No I/O. Blocks are API block objects; the fetcher attaches each block's children (when
`has_children`) under the CHILDREN key, so rendering is a plain recursive walk.
"""

import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ..base import Item
from ..documents import normalize_body

KIND = "notion_page"
CHILDREN = "_children"
TITLE_MAX = 200
# Pages with less rendered text than this are indexed for search but not worth paying
# LLM extraction for (empty stubs, a title and a link).
EXTRACT_MIN_CHARS = 200
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

_DASHED = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_COMPACT = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")


def dashed(compact: str) -> str:
    c = compact.replace("-", "").lower()
    return f"{c[:8]}-{c[8:12]}-{c[12:16]}-{c[16:20]}-{c[20:]}"


def parse_page_id(target: str) -> str | None:
    """A page id (with or without dashes) or a Notion page URL -> the dashed page id.
    For URLs the id is the last one in the path (`/Title-<32 hex>`); query strings and
    fragments (`?v=`, `#block`) name views and blocks, not the page."""
    text = target.strip().lower()
    if "://" in text or text.startswith(("www.", "notion.so", "notion.site")):
        text = re.split(r"[?#]", text, maxsplit=1)[0]
    found = [m.group(0) for m in re.finditer(f"{_DASHED.pattern}|{_COMPACT.pattern}", text)]
    return dashed(found[-1]) if found else None


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


# --- Items ----------------------------------------------------------------------------

DATABASE_PARENTS = {"data_source", "database"}


def person(user_id: str | None, users: dict[str, str]) -> str | None:
    return f"notion:{users.get(user_id) or user_id}" if user_id else None


def page_url(page: dict[str, Any]) -> str:
    return page.get("url") or f"https://www.notion.so/{page['id'].replace('-', '')}"


def normalize_page(
    page: dict[str, Any], blocks: list[dict[str, Any]], *, scope_key: str, users: dict[str, str]
) -> Item | None:
    """A Notion page -> one Item. None for trashed pages and non-pages."""
    if page.get("object") != "page" or is_trashed(page):
        return None
    title = title_of(page)[:TITLE_MAX] or "Untitled"
    parent_type, _ = parent_ref(page)
    header = properties_header(page, users) if parent_type in DATABASE_PARENTS else []
    text = "\n".join(header + ([""] if header else []) + render_blocks(blocks))
    body, truncated = normalize_body(text)
    people = [
        person((page.get(k) or {}).get("id"), users) for k in ("created_by", "last_edited_by")
    ]
    return Item(
        source="notion",
        scope_key=scope_key,
        external_id=f"notion:{page['id']}",
        kind=KIND,
        url=page_url(page),
        title=title,
        text=body,
        reference_time=parse_time(page.get("last_edited_time")),
        participants=tuple(dict.fromkeys(p for p in people if p)),
        # last_edited_time and the parent live in metadata, outside the content hash.
        metadata={
            "page_id": page["id"],
            "last_edited_time": page.get("last_edited_time"),
            "created_time": page.get("created_time"),
            "parent_type": parent_type,
            "truncated": truncated,
        },
        extract=len(body) >= EXTRACT_MIN_CHARS,
    )


# --- Parents --------------------------------------------------------------------------


def parent_ref(obj: dict[str, Any]) -> tuple[str, str | None]:
    """('page'|'data_source'|'database'|'block'|'workspace', id). A data source's own
    parent is its database; we skip to the database's parent when Notion gives it."""
    parent = obj.get("parent") or {}
    if obj.get("object") == "data_source" and obj.get("database_parent"):
        parent = obj["database_parent"]
    t = parent.get("type") or "workspace"
    short = t.removesuffix("_id")
    return short, parent.get(t) if t != "workspace" else None
