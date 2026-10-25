import pytest


async def test_health_needs_no_auth(client):
    r = await client.get("/health")
    assert r.status_code == 200


async def test_missing_token_is_401(client):
    r = await client.get("/me")
    assert r.status_code == 401


@pytest.mark.parametrize(
    "kwargs",
    [
        {"aud": "someone-else"},
        {"iss": "http://evil"},
        {"ttl": -60},
    ],
    ids=["wrong-audience", "wrong-issuer", "expired"],
)
async def test_bad_tokens_are_401(client, make_token, seed, kwargs):
    r = await client.get(
        "/me", headers={"Authorization": f"Bearer {make_token(seed.alice, **kwargs)}"}
    )
    assert r.status_code == 401


async def test_garbage_token_is_401(client):
    r = await client.get("/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
