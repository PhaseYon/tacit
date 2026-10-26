"""Backfill, reconcile, trees and the permission model against a fake Notion API, ending
with the leak tests that matter: hybrid search never shows a connection's pages to
another member unless the admin-owner marked that tree visible to the workspace."""

from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tacit_api.connectors.notion import backfill, sync
from tacit_api.db import dispose_engine, rls_session, worker_session
from tacit_api.search.hybrid import hybrid_search
from tacit_api.search.indexer import index_workspace

from .conftest import MIGRATOR_URL, Seed
from .notion_mock import FakeNotion, block, data_source, page


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


def oauth(bot: str, nws: str = "NW-SYNC", user: str = "nu-alice", **extra) -> dict:
    return {
        "access_token": f"ntn_{bot}",
        "refresh_token": f"ref_{bot}",
        "bot_id": bot,
        "workspace_id": nws,
        "workspace_name": "Acme Notion",
        "owner": {"type": "user", "user": {"object": "user", "id": user, "name": "Alice"}},
        **extra,
    }


async def connect(ws: str, user: str, bot: str, nws: str = "NW-SYNC", nuser: str = "nu-alice"):
    cid = await sync.upsert_connection(ws, user, oauth(bot, nws, nuser))
    return cid


async def run(ws: str, cid: str, mode: str = "full") -> int:
    """Drive a run to completion; returns the number of search pages."""
    pages, cont = 0, False
    while True:
        more = await sync.with_connection(
            ws, cid, lambda c, cl, k=cont: backfill.run_page(c, cl, mode=mode, continuation=k)
        )
        pages += 1
        if not more:
            return pages
        cont = True


async def docs(ws: str, cid: str) -> dict[str, tuple]:
    async with worker_session(ws) as s:
        rows = await s.execute(
            text(
                "SELECT split_part(d.external_id, ':', 2), d.title, d.body, d.deleted_at,"
                " c.name FROM source_document d JOIN source_container c ON c.id = d.container_id"
                " WHERE d.source = 'notion' AND d.external_id LIKE :p"
            ),
            {"p": f"{cid}:%"},
        )
        return {r[0]: tuple(r[1:]) for r in rows}


def workspace_tree(fake: FakeNotion) -> None:
    """Roadmap (root) > Queue ADR, and a Tasks database under Roadmap with one row;
    a separate root 'Private notes'."""
    fake.add(
        page("p-roadmap", "Roadmap", edited="2025-10-05T10:00:00.000Z"),
        page("p-adr", "Queue ADR", parent=("page", "p-roadmap"), edited="2025-10-04T10:00:00.000Z"),
        data_source("ds-tasks", "Tasks", "db-tasks", ("page", "p-roadmap")),
        page(
            "p-row",
            "Drop Redis",
            parent=("data_source", "ds-tasks"),
            edited="2025-10-03T10:00:00.000Z",
        ),
        page("p-notes", "Private notes", edited="2025-10-02T10:00:00.000Z"),
    )
    fake.children["p-roadmap"] = [
        block("paragraph", "Q4 roadmap"),
        block("child_page", title="Queue ADR"),
    ]
    fake.children["p-adr"] = [
        block("heading_2", "Decision"),
        block("paragraph", "Zephyrqueue replaces Redis for background jobs."),
        block("paragraph", "Second paragraph."),
        block("paragraph", "Third paragraph."),
    ]
    fake.children["p-row"] = [block("paragraph", "Row body")]
    fake.children["p-notes"] = [block("paragraph", "Salary thoughts")]
    fake.users = [{"object": "user", "id": "nu-alice", "type": "person", "name": "Alice"}]


async def test_backfill_paginates_builds_trees_and_is_idempotent(seed: Seed, notion_api):
    fake = notion_api
    workspace_tree(fake)
    fake.search_page_size = 2
    fake.children_page_size = 2
    cid = await connect(seed.acme, seed.alice, "bot-backfill")

    assert await run(seed.acme, cid) == 3  # 5 objects, 2 per search page
    got = await docs(seed.acme, cid)
    assert set(got) == {"p-roadmap", "p-adr", "p-row", "p-notes"}
    assert got["p-adr"][1] == (
        "## Decision\nZephyrqueue replaces Redis for background jobs.\n"
        "Second paragraph.\nThird paragraph."
    )  # block children were paginated (2 per page)
    assert got["p-adr"][3] == "Roadmap" and got["p-row"][3] == "Roadmap"  # same tree
    assert got["p-notes"][3] == "Private notes"
    assert all(c.token == "ntn_bot-backfill" for c in fake.calls_to("search"))

    # Re-running changes nothing and fetches no blocks: every page is unchanged.
    before = len(fake.calls_to("blocks/"))
    async with worker_session(seed.acme) as s:
        hashes = dict(
            (await s.execute(text(
                "SELECT external_id, content_hash FROM source_document WHERE external_id LIKE :p"
            ), {"p": f"{cid}:%"})).all()
        )  # fmt: skip
    await run(seed.acme, cid)
    assert len(fake.calls_to("blocks/")) == before
    assert await docs(seed.acme, cid) == got
    async with worker_session(seed.acme) as s:
        after = dict(
            (await s.execute(text(
                "SELECT external_id, content_hash FROM source_document WHERE external_id LIKE :p"
            ), {"p": f"{cid}:%"})).all()
        )  # fmt: skip
    assert after == hashes
    # The connector's directory was refreshed (author names).
    async with worker_session(seed.acme) as s:
        login = await s.scalar(
            text("SELECT login FROM external_identity WHERE external_id = 'NW-SYNC:nu-alice'")
        )
    assert login == "Alice"


async def test_edits_moves_trash_and_unshare(seed: Seed, notion_api):
    fake = notion_api
    workspace_tree(fake)
    cid = await connect(seed.acme, seed.alice, "bot-edits")
    await run(seed.acme, cid)
    await index_workspace(seed.acme)

    # Edit: newer last_edited_time + new text -> re-fetched by an incremental run.
    fake.objects["p-adr"]["last_edited_time"] = "2025-10-06T10:00:00.000Z"
    fake.children["p-adr"] = [block("paragraph", "Zephyrqueue was reverted.")]
    # Move: the ADR now lives under Private notes (a different tree).
    fake.objects["p-adr"]["parent"] = {"type": "page_id", "page_id": "p-notes"}
    await run(seed.acme, cid, mode="incremental")
    got = await docs(seed.acme, cid)
    assert got["p-adr"][1] == "Zephyrqueue was reverted." and got["p-adr"][3] == "Private notes"
    async with worker_session(seed.acme) as s:
        stale_chunks = await s.scalar(
            text(
                "SELECT count(*) FROM document_chunk k JOIN source_document d"
                " ON d.id = k.document_id WHERE d.external_id = :e"
                " AND k.container_id <> d.container_id"
            ),
            {"e": f"{cid}:p-adr"},
        )
    assert stale_chunks == 0  # chunks moved with the document (they carry the ACL)

    # Trash one page; unshare another (it vanishes from search and 404s).
    fake.objects["p-row"] |= {"in_trash": True, "last_edited_time": "2025-10-07T10:00:00.000Z"}
    del fake.objects["p-notes"]
    await run(seed.acme, cid, mode="full")
    got = await docs(seed.acme, cid)
    assert got["p-row"][2] is not None
    assert got["p-notes"][2] is not None
    # Its parent is gone: the ADR becomes its own (private) tree.
    assert got["p-adr"][2] is None and got["p-adr"][3] == "Queue ADR"

    # An empty tree is deactivated.
    async with worker_session(seed.acme) as s:
        active = set(
            (
                await s.execute(
                    text(
                        "SELECT c.name FROM source_container c JOIN notion_tree t"
                        " ON t.container_id = c.id WHERE t.connection_id = :c AND c.is_active"
                    ),
                    {"c": cid},
                )
            ).scalars()
        )
    assert active == {"Roadmap", "Queue ADR"}


async def test_page_edited_mid_scan_is_not_tombstoned(seed: Seed, notion_api):
    """A page missing from the scan but still readable is kept and re-synced."""
    fake = notion_api
    fake.add(page("p-mid", "Mid scan"))
    fake.children["p-mid"] = [block("paragraph", "v1")]
    cid = await connect(seed.acme, seed.alice, "bot-mid")
    await run(seed.acme, cid)
    fake.hidden_from_search.add("p-mid")  # skipped by the cursor, but still there
    await run(seed.acme, cid)
    assert (await docs(seed.acme, cid))["p-mid"][2] is None
    assert fake.calls_to("pages/p-mid")


async def test_reconnect_updates_and_stolen_bot_is_refused(seed: Seed, notion_api):
    cid = await connect(seed.acme, seed.alice, "bot-steal")
    again = await connect(seed.acme, seed.alice, "bot-steal")
    assert again == cid
    with pytest.raises(DBAPIError):
        await sync.upsert_connection(seed.globex, seed.carol, oauth("bot-steal"))
    with pytest.raises(sync.ConnectError):
        await sync.upsert_connection(
            seed.acme, seed.alice, oauth("bot-ws", owner={"type": "workspace", "workspace": True})
        )
    conn = await sync.load_connection(seed.acme, cid)
    assert conn.access_token == "ntn_bot-steal"
    async with worker_session(seed.acme) as s:
        blob = await s.scalar(
            text("SELECT access_token_encrypted FROM notion_connection WHERE id = :c"), {"c": cid}
        )
    assert b"ntn_" not in bytes(blob)


async def test_expired_token_refreshes_once_then_revokes(seed: Seed, notion_api):
    fake = notion_api
    fake.add(page("p-tok", "Token page"))
    cid = await connect(seed.acme, seed.alice, "bot-refresh")
    unauthorized = (
        401,
        {"object": "error", "status": 401, "code": "unauthorized", "message": "x"},
        {},
    )
    fake.failures["search"] = unauthorized
    fake.oauth = {"access_token": "ntn_rotated", "refresh_token": "ref_rotated"}

    def clear_after_refresh(request, original=fake.handle):
        if request.url.path.endswith("oauth/token"):
            fake.failures.pop("search", None)
        return original(request)

    fake.handle = clear_after_refresh  # type: ignore[method-assign]
    await run(seed.acme, cid)
    token_call = fake.calls_to("oauth/token")[0]
    assert token_call.body == {"grant_type": "refresh_token", "refresh_token": "ref_bot-refresh"}
    assert (await sync.load_connection(seed.acme, cid)).access_token == "ntn_rotated"
    assert fake.calls_to("search")[-1].token == "ntn_rotated"

    # Now the refresh itself is refused: the connection is revoked and content hidden.
    fake.failures["search"] = unauthorized
    fake.failures["oauth/token"] = (
        400,
        {"object": "error", "status": 400, "code": "invalid_request", "message": "x"},
        {},
    )
    assert (
        await sync.with_connection(seed.acme, cid, lambda c, cl: backfill.run_page(c, cl)) is None
    )
    assert await sync.load_connection(seed.acme, cid) is None
    assert all(d[2] is not None for d in (await docs(seed.acme, cid)).values())


# --- Permissions and leaks ------------------------------------------------------------


async def search(user_id: str, ws: str, q: str) -> list[str]:
    async with rls_session(user_id, ws) as s:
        return [h.url for h in await hybrid_search(s, q)]


async def test_pages_never_reach_other_members_unless_admin_owner_marks_them(
    seed: Seed, notion_api, client, make_token
):
    """End to end through the real search path (tacit_api + RLS)."""
    fake = notion_api
    fake.add(
        page("p-secret", "Acquisition plan"),
        page("p-handbook", "Engineering handbook", edited="2025-10-02T10:00:00.000Z"),
    )
    fake.children["p-secret"] = [block("paragraph", "Quokkacorp acquisition closes Friday")]
    fake.children["p-handbook"] = [block("paragraph", "Quokkacorp style guide for reviews")]
    alice_cid = await connect(seed.acme, seed.alice, "bot-leak-a")
    await run(seed.acme, alice_cid)
    await index_workspace(seed.acme)
    secret_url = fake.objects["p-secret"]["url"]
    handbook_url = fake.objects["p-handbook"]["url"]

    hits = await search(seed.alice, seed.acme, "Quokkacorp")
    assert secret_url in hits and handbook_url in hits
    # Bob is in the same Tacit workspace but didn't connect: sees nothing.
    assert not {secret_url, handbook_url} & set(await search(seed.bob, seed.acme, "Quokkacorp"))
    # Carol (another workspace) forging acme sees nothing either.
    assert await search(seed.carol, seed.acme, "Quokkacorp") == []

    alice = {"Authorization": f"Bearer {make_token(seed.alice)}"}
    bob = {"Authorization": f"Bearer {make_token(seed.bob)}"}
    status = (await client.get(f"/workspaces/{seed.acme}/notion", headers=alice)).json()
    trees = {t["title"]: t for t in status["trees"] if t["connection_id"] == alice_cid}
    handbook = trees["Engineering handbook"]["id"]
    assert trees["Acquisition plan"]["can_read"] and trees["Acquisition plan"]["documents"] == 1
    # Bob doesn't even learn the trees exist: titles are page content.
    bob_view = (await client.get(f"/workspaces/{seed.acme}/notion", headers=bob)).json()
    assert not [t for t in bob_view["trees"] if t["connection_id"] == alice_cid]
    async with rls_session(seed.bob, seed.acme) as s:
        names = set(
            (
                await s.execute(text("SELECT name FROM source_container WHERE source = 'notion'"))
            ).scalars()
        )
    assert not {"Acquisition plan", "Engineering handbook"} & names

    # Bob (member, not owner) can't widen it (he can't even find it); Alice (owner AND
    # admin) can.
    url = f"/workspaces/{seed.acme}/notion/trees/{handbook}/visibility"
    assert (await client.put(url, json={"workspace_visible": True}, headers=bob)).status_code == 404
    r = await client.put(url, json={"workspace_visible": True}, headers=alice)
    assert r.status_code == 200
    bob_hits = await search(seed.bob, seed.acme, "Quokkacorp")
    assert handbook_url in bob_hits and secret_url not in bob_hits  # only the marked tree
    assert await search(seed.carol, seed.globex, "Quokkacorp") == []

    # The hourly ACL refresh keeps the marking; a later sync doesn't undo it.
    await sync.sync_acl(seed.acme, alice_cid)
    await run(seed.acme, alice_cid)
    assert handbook_url in await search(seed.bob, seed.acme, "Quokkacorp")

    # Bob can narrow? No: he's neither owner nor admin. Alice unmarks: gone again.
    assert (
        await client.put(url, json={"workspace_visible": False}, headers=bob)
    ).status_code == 403
    await client.put(url, json={"workspace_visible": False}, headers=alice)
    assert handbook_url not in await search(seed.bob, seed.acme, "Quokkacorp")

    # Disconnect: nothing left for anyone, tokens wiped.
    r = await client.delete(
        f"/workspaces/{seed.acme}/notion/connections/{alice_cid}", headers=alice
    )
    assert r.status_code == 200
    assert not {secret_url, handbook_url} & set(await search(seed.alice, seed.acme, "Quokkacorp"))


async def test_admin_cannot_publish_a_members_pages(seed: Seed, notion_api, client, make_token):
    fake = notion_api
    fake.add(page("p-bob", "Bob diary"))
    fake.children["p-bob"] = [block("paragraph", "Wombatix private notes")]
    bob_cid = await connect(seed.acme, seed.bob, "bot-leak-b", nuser="nu-bob")
    await run(seed.acme, bob_cid)
    await index_workspace(seed.acme)
    bob_url = fake.objects["p-bob"]["url"]
    assert bob_url in await search(seed.bob, seed.acme, "Wombatix")
    assert bob_url not in await search(seed.alice, seed.acme, "Wombatix")  # even the owner role

    alice = {"Authorization": f"Bearer {make_token(seed.alice)}"}
    bob = {"Authorization": f"Bearer {make_token(seed.bob)}"}
    trees = (await client.get(f"/workspaces/{seed.acme}/notion", headers=bob)).json()["trees"]
    tree = next(t["id"] for t in trees if t["connection_id"] == bob_cid)
    url = f"/workspaces/{seed.acme}/notion/trees/{tree}/visibility"
    # Admin but not the owner: can't widen (the tree is invisible to her: 404). Owner but
    # not admin: can't widen either.
    assert (
        await client.put(url, json={"workspace_visible": True}, headers=alice)
    ).status_code == 404
    assert (await client.put(url, json={"workspace_visible": True}, headers=bob)).status_code == 403
    assert bob_url not in await search(seed.alice, seed.acme, "Wombatix")
    # Narrowing is always allowed for the owner.
    assert (
        await client.put(url, json={"workspace_visible": False}, headers=bob)
    ).status_code == 200


async def test_owner_who_leaves_workspace_loses_access(seed: Seed, notion_api):
    """Access rows require current membership; a stale ACL fails closed."""
    fake = notion_api
    fake.add(page("p-leaver", "Leaver page"))
    now = datetime.now(UTC)
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            'INSERT INTO "user" (id, name, email, "emailVerified") VALUES'
            " ('u_leaver', 'Leaver', 'leaver@example.com', true) ON CONFLICT DO NOTHING"
        )
        conn.execute(
            'INSERT INTO member (id, "organizationId", "userId", role, "createdAt") VALUES'
            " ('m_leaver', %s, 'u_leaver', 'member', %s) ON CONFLICT DO NOTHING",
            (seed.acme, now),
        )
    cid = await connect(seed.acme, "u_leaver", "bot-leaver", nuser="nu-leaver")
    await run(seed.acme, cid)
    assert any(t.startswith("Leaver") for t in await _trees_for(seed.acme, "u_leaver", cid))
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM member WHERE id = 'm_leaver'")
    await sync.sync_acl(seed.acme, cid)
    async with worker_session(seed.acme) as s:
        rows = await s.scalar(
            text(
                "SELECT count(*) FROM container_access a JOIN notion_tree t"
                " ON t.container_id = a.container_id WHERE t.connection_id = :c"
            ),
            {"c": cid},
        )
    assert rows == 0


async def _trees_for(ws: str, user: str, cid: str) -> set[str]:
    async with rls_session(user, ws) as s:
        rows = await s.execute(
            text(
                "SELECT t.title FROM notion_tree t WHERE t.connection_id = :c"
                " AND app.can_read_container(t.container_id)"
            ),
            {"c": cid},
        )
        return set(rows.scalars())
