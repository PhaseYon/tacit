"""MCP server: token audience checks and tools running with the caller's permissions."""

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tacit_api import mcp_server
from tacit_api.connectors.github import ingest, sync
from tacit_api.db import dispose_engine
from tacit_api.search.indexer import index_workspace

from .conftest import AUDIENCE, MIGRATOR_URL, Seed
from .github_mock import repo_json
from .test_search import doc

MCP_AUD = "http://localhost:8000/mcp"
INST = 9600


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


@pytest.fixture
def verifier(signing_key):
    pem = (
        signing_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return mcp_server.make_verifier(public_key=pem)


async def test_verifier_accepts_only_mcp_audience(verifier, make_token, seed):
    ok = await verifier.verify_token(make_token(seed.alice, aud=MCP_AUD))
    assert ok is not None and ok.claims["sub"] == seed.alice
    # A web-session token (aud=tacit-api) must not work against MCP.
    assert await verifier.verify_token(make_token(seed.alice, aud=AUDIENCE)) is None
    assert (
        await verifier.verify_token(make_token(seed.alice, aud=MCP_AUD, iss="http://evil")) is None
    )
    assert await verifier.verify_token(make_token(seed.alice, aud=MCP_AUD, ttl=-60)) is None


async def test_rest_api_rejects_mcp_tokens(client, make_token, seed):
    r = await client.get(
        "/me", headers={"Authorization": f"Bearer {make_token(seed.alice, aud=MCP_AUD)}"}
    )
    assert r.status_code == 401


@pytest.fixture(scope="module")
async def private_pr(seed: Seed) -> None:
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO github_installation (workspace_id, installation_id, account_id,"
            " account_login, account_type) VALUES (%s, %s, 1, 'acme', 'Organization')"
            " ON CONFLICT DO NOTHING",
            (seed.acme, INST),
        )
    repo = await sync.upsert_repo(seed.acme, INST, repo_json(61, "payments"))
    with psycopg.connect(MIGRATOR_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO container_access (container_id, user_id, workspace_id, permission)"
            " VALUES (%s, %s, %s, 'admin')",
            (repo.container_id, seed.alice, seed.acme),
        )
        conn.execute(
            "UPDATE source_container SET acl_synced_at = now() WHERE id = %s", (repo.container_id,)
        )
    pr = doc(
        "mcp-pr",
        "pull_request",
        "Adopt idempotency keys for refunds",
        "Prevents double refunds.",
        77,
    )
    comment = doc(
        "mcp-c1", "issue_comment", None, "Approved: Stripe recommends this.", parent_number=77
    )
    await ingest.upsert_documents(seed.acme, repo.container_id, [pr])
    await ingest.upsert_documents(seed.acme, repo.container_id, [comment])
    await index_workspace(seed.acme)
    await dispose_engine()


def as_user(monkeypatch, user_id: str | None) -> None:
    class Token:
        claims = {"sub": user_id} if user_id else {}

    monkeypatch.setattr(mcp_server, "get_access_token", lambda: Token() if user_id else None)


async def test_tools_require_a_token(monkeypatch, seed):
    as_user(monkeypatch, None)
    async with Client(mcp_server.mcp) as c:
        with pytest.raises(ToolError, match="Not authenticated"):
            await c.call_tool("list_workspaces", {})


async def test_list_workspaces(monkeypatch, seed):
    as_user(monkeypatch, seed.alice)
    async with Client(mcp_server.mcp) as c:
        ws = (await c.call_tool("list_workspaces", {})).data
    assert [w["slug"] for w in ws] == ["acme"]


async def test_search_and_read_thread_with_citations(monkeypatch, seed, private_pr):
    as_user(monkeypatch, seed.alice)
    async with Client(mcp_server.mcp) as c:
        res = (await c.call_tool("search", {"query": "double refunds idempotency"})).data
        top = res["results"][0]
        assert top["title"] == "Adopt idempotency keys for refunds"
        assert top["url"].startswith("https://github.com/")
        d = (await c.call_tool("get_document", {"document_id": top["document_id"]})).data
    assert d["document"]["title"] == "Adopt idempotency keys for refunds"
    assert [t["body"] for t in d["thread"]] == ["Approved: Stripe recommends this."]


async def test_private_content_hidden_from_users_without_access(monkeypatch, seed, private_pr):
    as_user(monkeypatch, seed.alice)
    async with Client(mcp_server.mcp) as c:
        doc_id = (await c.call_tool("search", {"query": "idempotency refunds"})).data["results"][0][
            "document_id"
        ]
    as_user(monkeypatch, seed.bob)  # same workspace, no access to the private repo
    async with Client(mcp_server.mcp) as c:
        titles = [
            r["title"]
            for r in (await c.call_tool("search", {"query": "idempotency refunds"})).data["results"]
        ]
        assert "Adopt idempotency keys for refunds" not in titles
        with pytest.raises(ToolError, match="Document not found"):
            await c.call_tool("get_document", {"document_id": doc_id})


async def test_cannot_pick_someone_elses_workspace(monkeypatch, seed):
    as_user(monkeypatch, seed.carol)
    async with Client(mcp_server.mcp) as c:
        with pytest.raises(ToolError, match="Workspace not found"):
            await c.call_tool("search", {"query": "x", "workspace": "acme"})
