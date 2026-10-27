from tacit import demo
from tacit.store import Store
from tacit.store import docs as d
from tacit.store import state as st


async def test_demo_loads_through_the_real_github_source(store: Store):
    r1 = await demo.load(store)
    assert r1.blocked_by is None
    written = {s.key: s.written for s in r1.scopes}
    assert set(written) == {
        "github:larkspur/api",
        "github:larkspur/handbook",
        "github:larkspur/web",
    }
    assert all(s.error is None and s.skipped_reason is None for s in r1.scopes)
    data = demo.load_data()
    expected = sum(
        len(x["issues"]) + len(x["pull_requests"]) + len(x["markdown_files"]) for x in data.values()
    )
    # Every issue and PR becomes an item; docs only when they match the doc globs.
    total = sum(written.values())
    assert expected - 10 <= total <= expected

    r2 = await demo.load(store)
    assert sum(s.written for s in r2.scopes) == 0
    assert sum(s.unchanged for s in r2.scopes) <= total

    doc = await d.get_doc(store, "https://github.com/larkspur/api/pull/1")
    assert doc and doc["title"] == "Bootstrap FastAPI service" and "@tomas-rivera" in doc["text"]
    scope = await st.scope_list(store)
    assert all(e.options.get("demo") for e in scope)


async def test_demo_docs_are_dated_by_their_own_history(store: Store):
    await demo.load(store)
    adr = await d.get_doc(
        store,
        "https://github.com/larkspur/handbook/blob/main/docs/adr/0012-postgres-full-text-search.md",
    )
    assert adr and adr["reference_time"].to_native().date().isoformat() == "2026-03-17"
