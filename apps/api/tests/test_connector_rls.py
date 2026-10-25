"""Leak tests for synced content (migration 0002).

The rules under test: users see a document only if it's in their workspace AND they
can read its container on the source (public, or a fresh access row). Workers see only
the workspace their job selected. Everything fails closed.
"""

from dataclasses import dataclass

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tacit_api.db import dispose_engine, rls_session, worker_session

from .conftest import MIGRATOR_URL, Seed


@dataclass(frozen=True)
class Containers:
    public: str = "00000000-0000-0000-0000-00000000a001"
    private_alice: str = "00000000-0000-0000-0000-00000000a002"  # fresh ACL: alice
    private_stale: str = "00000000-0000-0000-0000-00000000a003"  # stale ACL: alice + bob
    inactive: str = "00000000-0000-0000-0000-00000000a004"  # removed from install, public
    globex: str = "00000000-0000-0000-0000-00000000b001"  # other workspace, public


@pytest.fixture(scope="module")
def containers(seed: Seed) -> Containers:
    c = Containers()
    rows = [
        (c.public, seed.acme, "public", True, "now()"),
        (c.private_alice, seed.acme, "private", True, "now()"),
        (c.private_stale, seed.acme, "private", True, "now() - interval '7 hours'"),
        (c.inactive, seed.acme, "public", False, "now()"),
        (c.globex, seed.globex, "public", True, "now()"),
    ]
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        for cid, ws, vis, active, synced in rows:
            conn.execute(
                "INSERT INTO source_container (id, workspace_id, source, external_id, name,"
                f" visibility, is_active, acl_synced_at) VALUES (%s, %s, 'github', %s, %s, %s,"
                f" %s, {synced})",
                (cid, ws, cid, f"repo-{cid[-4:]}", vis, active),
            )
            conn.execute(
                "INSERT INTO source_document (workspace_id, container_id, source, kind,"
                " external_id, url, title, content_hash) VALUES (%s, %s, 'github', 'issue',"
                " %s, %s, %s, '\\x00')",
                (ws, cid, f"doc-{cid}", f"https://github.com/x/{cid}", f"doc in {cid[-4:]}"),
            )
        for cid, uid in [
            (c.private_alice, seed.alice),
            (c.private_stale, seed.alice),
            (c.private_stale, seed.bob),
        ]:
            conn.execute(
                "INSERT INTO container_access (container_id, user_id, workspace_id, permission)"
                " VALUES (%s, %s, %s, 'read')",
                (cid, uid, seed.acme),
            )
    return c


OWN = set(Containers().__dict__.values())  # other modules add containers to the same workspace


async def _visible_containers(user_id: str, workspace_id: str | None) -> set[str]:
    async with rls_session(user_id, workspace_id) as s:
        rows = await s.execute(text("SELECT container_id::text FROM source_document"))
        return set(rows.scalars()) & OWN


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


async def test_user_sees_public_and_own_fresh_private(seed, containers):
    c = containers
    assert await _visible_containers(seed.alice, seed.acme) == {c.public, c.private_alice}


async def test_stale_acl_hides_private_content(seed, containers):
    # Bob's only private access row is 7 hours old, past the 6-hour cutoff.
    assert await _visible_containers(seed.bob, seed.acme) == {containers.public}


async def test_no_workspace_selected_sees_nothing(seed, containers):
    assert await _visible_containers(seed.alice, None) == set()


async def test_non_member_cannot_select_other_workspace(seed, containers):
    # Carol forges workspace_id = acme; is_member() still says no.
    assert await _visible_containers(seed.carol, seed.acme) == set()


async def test_other_workspace_is_separate(seed, containers):
    assert await _visible_containers(seed.carol, seed.globex) == {containers.globex}


async def test_access_rows_scoped_to_workspace(seed, containers):
    async with rls_session(seed.carol, seed.globex) as s:
        rows = (await s.execute(text("SELECT 1 FROM container_access"))).all()
    assert rows == []


async def test_api_role_cannot_write_documents(seed, containers):
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with rls_session(seed.alice, seed.acme) as s:
            await s.execute(text("UPDATE source_document SET title = 'x'"))


async def test_worker_sees_only_selected_workspace(seed, containers):
    c = containers
    async with worker_session(seed.acme) as s:
        got = set((await s.execute(text("SELECT id::text FROM source_container"))).scalars()) & OWN
    assert got == {c.public, c.private_alice, c.private_stale, c.inactive}

    async with worker_session() as s:
        got = (await s.execute(text("SELECT 1 FROM source_document"))).all()
    assert got == []


async def test_worker_cannot_write_into_other_workspace(seed, containers):
    with pytest.raises(DBAPIError, match="row-level security"):
        async with worker_session(seed.acme) as s:
            await s.execute(
                text(
                    "INSERT INTO external_identity (workspace_id, source, external_id)"
                    " VALUES (:ws, 'github', '1')"
                ),
                {"ws": seed.globex},
            )


async def test_worker_reads_linked_accounts_but_not_tokens(seed, containers):
    async with worker_session(seed.acme) as s:
        await s.execute(text('SELECT "userId", "providerId", "accountId" FROM account'))
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with worker_session(seed.acme) as s:
            await s.execute(text('SELECT "accessToken" FROM account'))


async def test_worker_resolves_installation_workspace(seed, containers):
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO github_installation (workspace_id, installation_id, account_id,"
            " account_login, account_type) VALUES (%s, 4242, 1, 'acme', 'Organization')"
            " ON CONFLICT DO NOTHING",
            (seed.acme,),
        )
    async with worker_session() as s:
        ws = await s.scalar(text("SELECT app.workspace_for_installation(4242)"))
        missing = await s.scalar(text("SELECT app.workspace_for_installation(999)"))
        # Without a workspace selected, the worker can't read the table directly.
        direct = (await s.execute(text("SELECT 1 FROM github_installation"))).all()
    assert ws == seed.acme
    assert missing is None
    assert direct == []
