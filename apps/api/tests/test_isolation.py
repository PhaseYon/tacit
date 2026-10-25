"""Workspace isolation, checked at both the API and the Postgres (RLS) level."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from tacit_api.db import dispose_engine, get_engine, rls_session


def auth(make_token, user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(user_id)}"}


async def test_me_lists_only_own_workspaces(client, make_token, seed):
    r = await client.get("/me", headers=auth(make_token, seed.alice))
    assert r.status_code == 200
    assert [w["id"] for w in r.json()["workspaces"]] == [seed.acme]
    assert r.json()["workspaces"][0]["role"] == "owner"


async def test_members_of_own_workspace(client, make_token, seed):
    r = await client.get(f"/workspaces/{seed.acme}/members", headers=auth(make_token, seed.bob))
    assert r.status_code == 200
    assert {m["user_id"] for m in r.json()} == {seed.alice, seed.bob}


async def test_other_workspace_is_404(client, make_token, seed):
    r = await client.get(f"/workspaces/{seed.globex}/members", headers=auth(make_token, seed.alice))
    assert r.status_code == 404


async def test_unknown_user_sees_nothing(client, make_token, seed):
    r = await client.get("/me", headers=auth(make_token, "u_nobody"))
    assert r.status_code == 200
    assert r.json()["workspaces"] == []


# --- Postgres-level checks: these hold even if an app-level filter is forgotten ---


async def _ids(sql: str, user_id: str, workspace_id: str | None = None) -> set[str]:
    async with rls_session(user_id, workspace_id) as s:
        return set((await s.execute(text(sql))).scalars())


async def test_rls_unfiltered_queries_are_scoped(seed):
    try:
        assert await _ids("SELECT id FROM organization", seed.alice) == {seed.acme}
        assert await _ids('SELECT "userId" FROM member', seed.alice) == {seed.alice, seed.bob}
        assert await _ids('SELECT id FROM "user"', seed.alice) == {seed.alice, seed.bob}
        assert await _ids("SELECT id FROM invitation", seed.alice) == set()
        assert await _ids("SELECT id FROM invitation", seed.carol) == {"inv1"}
    finally:
        await dispose_engine()


async def test_rls_without_user_context_sees_nothing(seed):
    try:
        async with get_engine().connect() as conn:
            rows = (await conn.execute(text("SELECT id FROM organization"))).all()
        assert rows == []
    finally:
        await dispose_engine()


async def test_rls_context_does_not_leak_across_transactions(seed):
    """set_config(..., is_local) must reset at commit, even on a reused pooled connection."""
    try:
        engine = get_engine()
        async with rls_session(seed.alice):
            pass
        async with engine.connect() as conn:
            uid = await conn.scalar(text("SELECT current_setting('app.user_id', true)"))
            orgs = (await conn.execute(text("SELECT id FROM organization"))).all()
        assert not uid
        assert orgs == []
    finally:
        await dispose_engine()


@pytest.mark.parametrize("table", ["session", "account", "verification", "jwks"])
async def test_api_role_cannot_read_secrets(seed, table):
    try:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await _ids(f'SELECT id FROM "{table}"', seed.alice)
    finally:
        await dispose_engine()


async def test_api_role_cannot_read_password_hashes_or_write(seed):
    try:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await _ids('SELECT "emailVerified" FROM "user"', seed.alice)
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with rls_session(seed.alice) as s:
                await s.execute(text("UPDATE organization SET name = 'pwned'"))
    finally:
        await dispose_engine()
