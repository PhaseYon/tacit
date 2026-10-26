"""Pure normalizer tests: block rendering, titles, properties, hashing, tree roots."""

from tacit_api.connectors.notion import normalize as n

from .notion_mock import block, data_source, page, rt

CTX = n.Context("conn1", "NW1", {"nu-alice": "Alice", "nu-bob": "Bob"})


def with_children(b: dict, *kids: dict) -> dict:
    return b | {"has_children": True, n.CHILDREN: list(kids)}


def test_blocks_render_to_markdownish_text():
    blocks = [
        block("heading_1", "Decision"),
        block("paragraph", "We picked Postgres queues over Redis."),
        block("heading_2", "Why"),
        with_children(
            block("bulleted_list_item", "Fewer moving parts"), block("bulleted_list_item", "nested")
        ),
        block("numbered_list_item", "First"),
        block("numbered_list_item", "Second"),
        block("to_do", "Migrate workers", checked=True),
        block("to_do", "Delete Redis", checked=False),
        with_children(block("toggle", "Details"), block("paragraph", "hidden text")),
        block("quote", "Simple beats clever"),
        block("callout", "Heads up", icon={"type": "emoji", "emoji": "⚠️"}),
        block("code", "SELECT 1;\nSELECT 2;", language="sql"),
        block("equation", expression="e=mc^2"),
        block("divider"),
        block("bookmark", url="https://example.com/rfc"),
        block("image", caption=rt("Arch diagram"), name="arch.png"),
        block("child_page", title="Appendix"),
        block("child_database", title="Tasks"),
        block("table_of_contents"),
        block("unsupported"),
    ]
    lines = n.render_blocks(blocks)
    assert lines == [
        "# Decision",
        "We picked Postgres queues over Redis.",
        "## Why",
        "- Fewer moving parts",
        "  - nested",
        "1. First",
        "2. Second",
        "- [x] Migrate workers",
        "- [ ] Delete Redis",
        "Details",
        "  hidden text",
        "> Simple beats clever",
        "> ⚠️ Heads up",
        "```sql",
        "SELECT 1;",
        "SELECT 2;",
        "```",
        "$$e=mc^2$$",
        "---",
        "https://example.com/rfc",
        "[image: arch.png] Arch diagram",
        "[page: Appendix]",
        "[database: Tasks]",
    ]


def test_tables_columns_synced_blocks_and_links():
    table = with_children(
        block("table", table_width=2),
        {"type": "table_row", "table_row": {"cells": [rt("Option"), rt("Verdict")]}},
        {"type": "table_row", "table_row": {"cells": [rt("Redis"), rt("No")]}},
    )
    columns = with_children(
        block("column_list"),
        with_children(block("column"), block("paragraph", "left")),
        with_children(block("column"), block("paragraph", "right")),
    )
    synced = with_children(block("synced_block"), block("paragraph", "shared snippet"))
    link = {"type": "paragraph", "paragraph": {"rich_text": rt("the RFC", "https://x.test/rfc")}}
    assert n.render_blocks([table, columns, synced, link]) == [
        "Option | Verdict",
        "Redis | No",
        "left",
        "right",
        "shared snippet",
        "the RFC (https://x.test/rfc)",
    ]


def test_page_document_fields_and_citation():
    p = page("p-1", "Queue decision", author="nu-bob")
    doc = n.normalize_page(p, [block("paragraph", "Use Postgres.")], CTX)
    assert doc is not None
    assert doc.kind == "notion_page" and doc.title == "Queue decision"
    assert doc.external_id == "conn1:p-1"
    assert doc.url == p["url"]
    assert doc.body == "Use Postgres."
    assert doc.author_external_id == "NW1:nu-bob" and doc.author_login == "Bob"
    assert doc.source_updated_at.isoformat() == "2025-10-01T10:00:00+00:00"


def test_database_row_gets_stable_property_header():
    props = {
        "Status": {"type": "status", "status": {"name": "Decided"}},
        "Owner": {"type": "people", "people": [{"id": "nu-alice", "name": "Alice"}]},
        "Tags": {"type": "multi_select", "multi_select": [{"name": "infra"}, {"name": "db"}]},
        "Due": {"type": "date", "date": {"start": "2026-11-01", "end": None}},
        "Done": {"type": "checkbox", "checkbox": True},
        "ID": {"type": "unique_id", "unique_id": {"prefix": "ADR", "number": 7}},
        "Score": {"type": "formula", "formula": {"type": "number", "number": 3}},
        "Edited": {"type": "last_edited_time", "last_edited_time": "2026-10-01T10:00:00Z"},
    }
    row = page("r-1", "Use Postgres", parent=("data_source", "ds-1"), props=props)
    doc = n.normalize_page(row, [block("paragraph", "Context here.")], CTX)
    assert doc.body.split("\n") == [
        "Done: Yes",
        "Due: 2026-11-01",
        "ID: ADR-7",
        "Owner: Alice",
        "Status: Decided",
        "Tags: infra, db",
        "",
        "Context here.",
    ]  # formula and last_edited_time are left out


def test_hash_ignores_edit_time_noise_but_tracks_text():
    a = n.normalize_page(
        page("p", "T", edited="2026-10-01T10:00:00.000Z"), [block("paragraph", "x")], CTX
    )
    b = n.normalize_page(
        page("p", "T", edited="2026-10-02T11:00:00.000Z"), [block("paragraph", "x")], CTX
    )
    c = n.normalize_page(page("p", "T"), [block("paragraph", "y")], CTX)
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


def test_trashed_and_untitled_pages():
    assert n.normalize_page(page("p", "Gone", in_trash=True), [], CTX) is None
    assert n.normalize_page(page("p", "Gone", archived=True), [], CTX) is None
    assert n.normalize_page(page("p", ""), [], CTX).title == "Untitled"


def test_parent_refs():
    assert n.parent_ref(page("p", "x")) == ("workspace", None)
    assert n.parent_ref(page("p", "x", parent=("page", "q"))) == ("page", "q")
    assert n.parent_ref(page("p", "x", parent=("block", "b"))) == ("block", "b")
    ds = data_source("ds", "Tasks", "db", ("page", "q"))
    assert n.parent_ref(ds) == ("page", "q")  # skips the database to its parent


def test_tree_roots():
    nodes = [
        n.Node("root", "page", "workspace", None),
        n.Node("child", "page", "page", "root"),
        n.Node("grandchild", "page", "page", "child"),
        n.Node("ds", "data_source", "page", "child", database_id="db"),
        n.Node("row", "page", "data_source", "ds"),
        n.Node("row-old-api", "page", "database", "db"),
        n.Node("orphan", "page", "page", "not-shared"),  # parent not visible to us
        n.Node("in-column", "page", "block", "blk"),  # block parent: unresolved
        n.Node("loop-a", "page", "page", "loop-b"),
        n.Node("loop-b", "page", "page", "loop-a"),
    ]
    roots = n.compute_roots(nodes)
    for nid in ("root", "child", "grandchild", "ds", "row", "row-old-api"):
        assert roots[nid] == "root"
    assert roots["orphan"] == "orphan" and roots["in-column"] == "in-column"
    assert roots["loop-a"] == "loop-a" and roots["loop-b"] == "loop-b"
