"""Connect + setup flow and the status endpoint."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import psycopg
import pytest
import respx

from tacit_api.connectors.github import client, routes, tasks

from .conftest import MIGRATOR_URL, Seed
from .github_mock import API, mock_installation_token, write_app_key

INST = 9400
GH_ALICE = 101  # same linked account as test_github_sync


@pytest.fixture(autouse=True)
def github_settings(monkeypatch, tmp_path):
    from tacit_api.config import get_settings

    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_SLUG", "tacit-test")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(write_app_key(tmp_path)))
    monkeypatch.setenv("GITHUB_CACHE", "memory")
    monkeypatch.setenv("BETTER_AUTH_SECRET", "test-secret-that-is-long-enough-for-hs256")
    monkeypatch.setenv("WEB_ORIGIN", "http://web.test")
    get_settings.cache_clear()
    client._private_key.cache_clear()
    yield
    get_settings.cache_clear()
    client._private_key.cache_clear()


@pytest.fixture
def enqueued(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(tasks.sync_installation, "delay", lambda *a: calls.append(a))
    return calls


@pytest.fixture(scope="module")
def alice_linked(seed: Seed) -> Seed:
    now = datetime.now(UTC)
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            'INSERT INTO account (id, "accountId", "providerId", "userId", "createdAt",'
            " \"updatedAt\") VALUES ('acc_u_alice', %s, 'github', %s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (str(GH_ALICE), seed.alice, now, now),
        )
    return seed


def auth(make_token, uid):
    return {"Authorization": f"Bearer {make_token(uid)}"}


async def test_connect_requires_admin(client, make_token, seed):
    r = await client.post(
        f"/workspaces/{seed.acme}/github/connect", json={}, headers=auth(make_token, seed.bob)
    )
    assert r.status_code == 403


async def test_connect_requires_linked_github(client, make_token, seed):
    # Use a fresh workspace owner with no linked account.
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            'INSERT INTO member (id, "organizationId", "userId", role, "createdAt")'
            " VALUES ('m_carol_owner_g', %s, %s, 'owner', now()) ON CONFLICT DO NOTHING",
            (seed.globex, seed.carol),
        )
    r = await client.post(
        f"/workspaces/{seed.globex}/github/connect", json={}, headers=auth(make_token, seed.carol)
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "github_not_linked"


async def test_connect_returns_install_url_with_signed_state(client, make_token, alice_linked):
    s = alice_linked
    r = await client.post(
        f"/workspaces/{s.acme}/github/connect",
        json={"return_to": "/w/acme/settings"},
        headers=auth(make_token, s.alice),
    )
    assert r.status_code == 200
    url = urlparse(r.json()["url"])
    assert url.path == "/apps/tacit-test/installations/new"
    state = routes.read_state(parse_qs(url.query)["state"][0])
    assert (state["ws"], state["sub"], state["rt"]) == (s.acme, s.alice, "/w/acme/settings")


def _mock_install(router, account_id: int):
    mock_installation_token(router, INST)
    router.get(f"{API}/app/installations/{INST}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": INST,
                "account": {"id": account_id, "login": "alice", "type": "User"},
                "target_type": "User",
                "repository_selection": "all",
            },
        )
    )


@respx.mock
async def test_setup_links_installation_and_starts_sync(client, alice_linked, enqueued):
    s = alice_linked
    _mock_install(respx.mock, GH_ALICE)
    state = routes.make_state(s.acme, s.alice, "/w/acme/settings")
    r = await client.get(
        "/github/setup", params={"state": state, "installation_id": INST, "setup_action": "install"}
    )
    assert r.status_code == 307
    assert r.headers["location"] == "http://web.test/w/acme/settings?github=connected"
    assert enqueued == [(s.acme, INST)]


@respx.mock
async def test_setup_rejects_someone_elses_installation(client, alice_linked, enqueued):
    """The installation_id in the redirect is attacker-controlled."""
    s = alice_linked
    _mock_install(respx.mock, account_id=999)  # installed on another GitHub account
    state = routes.make_state(s.acme, s.alice, "/w/acme/settings")
    r = await client.get("/github/setup", params={"state": state, "installation_id": INST + 1})
    assert "github_error=" in r.headers["location"]
    assert enqueued == []


async def test_setup_rejects_forged_or_expired_state(client, enqueued):
    forged = jwt.encode(
        {"aud": routes.STATE_AUDIENCE, "ws": "x", "sub": "y", "rt": "/", "exp": 9999999999},
        "not-our-secret-but-also-32-bytes-long!!",
        algorithm="HS256",
    )
    r = await client.get("/github/setup", params={"state": forged, "installation_id": INST})
    assert r.headers["location"].endswith("github_error=expired")
    assert enqueued == []


async def test_status_lists_repos_with_per_user_access(client, make_token, seed):
    r = await client.get(f"/workspaces/{seed.acme}/github", headers=auth(make_token, seed.alice))
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert all({"full_name", "can_read", "documents"} <= set(repo) for repo in body["repos"])

    r = await client.get(f"/workspaces/{seed.acme}/github", headers=auth(make_token, seed.carol))
    assert r.status_code == 404
