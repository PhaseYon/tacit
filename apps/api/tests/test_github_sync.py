"""Installation verification, repo sync and permission (ACL) sync against a mocked API."""

from datetime import UTC, datetime

import httpx
import psycopg
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tacit_api.connectors.github import client, sync
from tacit_api.db import dispose_engine, rls_session, worker_session

from .conftest import MIGRATOR_URL, Seed
from .github_mock import API, mock_installation_token, repo_json, write_app_key

INST = 9001
GH_ALICE, GH_BOB, GH_CAROL, GH_STRANGER = 101, 102, 103, 104


@pytest.fixture(autouse=True)
def github_settings(monkeypatch, tmp_path):
    from tacit_api.config import get_settings

    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(write_app_key(tmp_path)))
    monkeypatch.setenv("GITHUB_CACHE", "memory")
    get_settings.cache_clear()
    client._private_key.cache_clear()
    yield
    get_settings.cache_clear()
    client._private_key.cache_clear()


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


@pytest.fixture(scope="module")
def linked_accounts(seed: Seed) -> Seed:
    """Alice, Bob and Carol have linked GitHub accounts. Carol isn't in Acme."""
    now = datetime.now(UTC)
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        for uid, gid in [(seed.alice, GH_ALICE), (seed.bob, GH_BOB), (seed.carol, GH_CAROL)]:
            conn.execute(
                'INSERT INTO account (id, "accountId", "providerId", "userId", "accessToken",'
                " \"createdAt\", \"updatedAt\") VALUES (%s, %s, 'github', %s, 'secret', %s, %s)"
                " ON CONFLICT DO NOTHING",
                (f"acc_{uid}", str(gid), uid, now, now),
            )
    return seed


def _installation(account_type: str, account_id: int, login: str) -> dict:
    return {
        "id": INST,
        "account": {"id": account_id, "login": login, "type": account_type},
        "target_type": account_type,
        "repository_selection": "selected",
        "permissions": {"issues": "read"},
        "events": ["issues"],
    }


# --- install verification -----------------------------------------------------------


@respx.mock
async def test_personal_install_must_be_users_own_account():
    respx.get(f"{API}/app/installations/{INST}").mock(
        return_value=httpx.Response(200, json=_installation("User", GH_ALICE, "alice"))
    )
    mock_installation_token(respx.mock, INST)
    app_gh, inst_gh = client.app_client(), client.installation_client(INST)
    inst = await sync.verify_installer(app_gh, inst_gh, INST, GH_ALICE)
    assert inst["account"]["login"] == "alice"
    with pytest.raises(sync.InstallVerificationError):
        await sync.verify_installer(app_gh, inst_gh, INST, GH_BOB)


@pytest.mark.parametrize(
    ("status", "body", "ok"),
    [
        (200, {"state": "active", "role": "admin"}, True),
        (200, {"state": "active", "role": "member"}, False),
        (200, {"state": "pending", "role": "admin"}, False),
        (404, {"message": "Not Found"}, False),
    ],
    ids=["admin", "member", "pending-admin", "not-in-org"],
)
@respx.mock
async def test_org_install_requires_active_admin(status, body, ok):
    respx.get(f"{API}/app/installations/{INST}").mock(
        return_value=httpx.Response(200, json=_installation("Organization", 5, "acme"))
    )
    mock_installation_token(respx.mock, INST)
    respx.get(f"{API}/user/{GH_ALICE}").mock(
        return_value=httpx.Response(200, json={"id": GH_ALICE, "login": "alice"})
    )
    respx.get(f"{API}/orgs/acme/memberships/alice").mock(
        return_value=httpx.Response(status, json=body)
    )
    call = sync.verify_installer(
        client.app_client(), client.installation_client(INST), INST, GH_ALICE
    )
    if ok:
        await call
    else:
        with pytest.raises(sync.InstallVerificationError):
            await call


async def test_linked_github_id(linked_accounts):
    s = linked_accounts
    assert await sync.linked_github_id(s.acme, s.alice) == GH_ALICE
    assert await sync.linked_github_id(s.acme, "u_nobody") is None


async def test_installation_cannot_be_stolen_by_another_workspace(seed):
    inst = _installation("Organization", 5, "acme") | {"id": 9100}
    await sync.upsert_installation(seed.acme, inst, seed.alice)
    await sync.upsert_installation(seed.acme, inst, seed.alice)  # idempotent
    with pytest.raises(DBAPIError, match="row-level security"):
        await sync.upsert_installation(seed.globex, inst, seed.carol)
    async with worker_session() as s:
        assert await s.scalar(text("SELECT app.workspace_for_installation(9100)")) == seed.acme


# --- repo + ACL sync ------------------------------------------------------------------


@respx.mock
async def test_repo_and_acl_sync_end_to_end(linked_accounts):
    s = linked_accounts
    await sync.upsert_installation(s.acme, _installation("Organization", 5, "acme"), s.alice)
    mock_installation_token(respx.mock, INST)
    repos_route = respx.get(f"{API}/installation/repositories")
    repos_route.mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 2,
                "repositories": [repo_json(1, "backend"), repo_json(2, "site", private=False)],
            },
        )
    )
    gh = client.installation_client(INST)
    refs = {r.full_name: r for r in await sync.sync_installation_repos(s.acme, INST, gh)}
    assert set(refs) == {"acme/backend", "acme/site"}
    backend = refs["acme/backend"]

    respx.get(f"{API}/repos/acme/backend/collaborators").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": GH_ALICE, "login": "alice", "role_name": "admin"},
                {"id": GH_CAROL, "login": "carol", "role_name": "write"},  # not in Acme
                {"id": GH_STRANGER, "login": "stranger", "role_name": "read"},  # not linked
            ],
        )
    )
    assert await sync.sync_repo_acl(s.acme, backend, gh) == 1

    # A private doc in backend: Alice can read it, Bob can't.
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO source_document (workspace_id, container_id, source, kind, external_id,"
            " url, content_hash) VALUES (%s, %s, 'github', 'issue', 'I_backend_1',"
            " 'https://github.com/acme/backend/issues/1', '\\x00')",
            (s.acme, backend.container_id),
        )

    async def visible(user_id: str) -> int:
        async with rls_session(user_id, s.acme) as sess:
            return len(
                (
                    await sess.execute(
                        text("SELECT 1 FROM source_document WHERE external_id = 'I_backend_1'")
                    )
                ).all()
            )

    assert await visible(s.alice) == 1
    assert await visible(s.bob) == 0

    # Alice loses GitHub access: the next ACL sync removes her row.
    respx.get(f"{API}/repos/acme/backend/collaborators").mock(
        return_value=httpx.Response(200, json=[{"id": GH_BOB, "login": "bob"}])
    )
    await sync.sync_repo_acl(s.acme, backend, gh)
    assert await visible(s.alice) == 0
    assert await visible(s.bob) == 1

    # backend is removed from the installation: access and content disappear at once.
    repos_route.mock(
        return_value=httpx.Response(
            200, json={"total_count": 1, "repositories": [repo_json(2, "site", private=False)]}
        )
    )
    await sync.sync_installation_repos(s.acme, INST, gh)
    assert await visible(s.bob) == 0
    async with worker_session(s.acme) as sess:
        row = (
            await sess.execute(
                text(
                    "SELECT c.is_active, d.deleted_at IS NOT NULL AS tomb,"
                    " (SELECT count(*) FROM container_access a WHERE a.container_id = c.id) AS acl"
                    " FROM source_container c JOIN source_document d ON d.container_id = c.id"
                    " WHERE c.id = :cid"
                ),
                {"cid": backend.container_id},
            )
        ).one()
    assert (row.is_active, row.tomb, row.acl) == (False, True, 0)


@respx.mock
async def test_failed_acl_fetch_leaves_previous_state(linked_accounts):
    """A GitHub error mid-sync must not wipe or half-write access rows."""
    s = linked_accounts
    await sync.upsert_installation(s.acme, _installation("Organization", 5, "acme"), s.alice)
    mock_installation_token(respx.mock, INST)
    respx.get(f"{API}/installation/repositories").mock(
        return_value=httpx.Response(
            200, json={"total_count": 1, "repositories": [repo_json(3, "infra")]}
        )
    )
    gh = client.installation_client(INST)
    (infra,) = await sync.sync_installation_repos(s.acme, INST, gh)
    respx.get(f"{API}/repos/acme/infra/collaborators").mock(
        return_value=httpx.Response(200, json=[{"id": GH_ALICE, "login": "alice"}])
    )
    await sync.sync_repo_acl(s.acme, infra, gh)
    respx.get(f"{API}/repos/acme/infra/collaborators").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(Exception):  # noqa: B017
        await sync.sync_repo_acl(s.acme, infra, gh)
    async with worker_session(s.acme) as sess:
        n = await sess.scalar(
            text("SELECT count(*) FROM container_access WHERE container_id = :c"),
            {"c": infra.container_id},
        )
    assert n == 1  # unchanged; it will go stale after 6h if errors persist (fail closed)
