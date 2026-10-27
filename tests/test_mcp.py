import json
import sys
from datetime import UTC, datetime

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tacit import mcp as m
from tacit.sources.base import Item, ScopeEntry
from tacit.store import Store
from tacit.store import state as st
from tacit.sync import index_item


def _item(n: int, text: str) -> Item:
    return Item(
        source="github",
        scope_key="github:acme/api",
        external_id=f"github:PR_{n}",
        kind="pull_request",
        url=f"https://github.com/acme/api/pull/{n}",
        title=f"PR {n}",
        text=text,
        reference_time=datetime(2026, 3, n, tzinfo=UTC),
        participants=("github:dana",),
    )


@pytest.fixture
async def seeded(store: Store):
    await st.scope_add(store, ScopeEntry("github:acme/api", "github", "acme/api", "acme/api"))
    await st.cursor_set(store, "github:acme/api", "c1")
    for item in (
        _item(1, "We moved the api cache to Redis because Postgres was too slow."),
        _item(2, "Billing events go through Kafka."),
    ):
        await index_item(store, item)
    return store


async def test_tools_listed_with_guidance(store: Store):
    async with Client(m.build_server()) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"search", "get_document", "status"}
        assert "cite" in tools["search"].description
        assert "access log" in tools["search"].description
        props = tools["search"].inputSchema["properties"]
        assert set(props) == {"query", "limit", "as_of"}  # ctx is hidden from the model
        assert client.server_info.name == "tacit"
        assert "cite" in client.instructions and "point-in-time" in client.instructions


async def test_search_get_document_and_status(seeded: Store):
    async with Client(m.build_server()) as client:
        res = await client.call_tool("search", {"query": "why Redis?", "limit": 5})
        data = res.structured_content
        assert data["results"][0]["url"] == "https://github.com/acme/api/pull/1"
        assert data["facts"] == []

        res = await client.call_tool("get_document", {"url_or_id": data["results"][0]["url"]})
        doc = res.structured_content
        assert doc["text"].startswith("We moved the api cache to Redis")
        assert doc["reference_time"].startswith("2026-03-01") and doc["truncated"] is False

        with pytest.raises(ToolError, match="No stored document"):
            await client.call_tool("get_document", {"url_or_id": "https://nope"})
        with pytest.raises(ToolError, match="ISO date"):
            await client.call_tool("search", {"query": "x", "as_of": "yesterday"})

        status = (await client.call_tool("status", {})).structured_content
        assert status["graph"] == {"host": "localhost", "database": "neo4j"}
        assert status["items"] == 2
        assert status["sources"][0]["source"] == "github" and status["sources"][0]["items"] == 2
        scope = status["scopes"][0]
        assert scope["key"] == "github:acme/api" and scope["last_sync"]
        assert scope["last_sync_by"] == "tester@example.com"
        assert status["facts"] == {"total": 0, "current": 0}
        assert status["pending_extraction"] == 2

    tools = {e["tool"] for e in await st.access_log(seeded, grep="mcp.")}
    assert {"mcp.search", "mcp.get_document", "mcp.status"} <= tools


def test_install_snippets(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_is_wsl", lambda: False)
    monkeypatch.delenv("TACIT_CONFIG_DIR", raising=False)
    out = m.install("cursor")
    snippet = json.loads(out[out.index("{") :])
    assert snippet == {
        "mcpServers": {"tacit": {"command": sys.executable, "args": ["-m", "tacit", "mcp"]}}
    }
    assert "claude mcp add --scope user tacit -- " in m.install("claude-code")
    assert sys.executable in m.install("claude-code")
    with pytest.raises(ValueError):
        m.install("vscode")

    monkeypatch.setattr(m, "_is_wsl", lambda: True)
    monkeypatch.setattr(m, "claude_desktop_config_path", lambda: None)
    out = m.install("claude")
    entry = json.loads(out[out.index("{") :])["mcpServers"]["tacit"]
    assert entry["command"] == "wsl.exe" and entry["args"][:2] == ["-e", sys.executable]


def test_install_write_merges_without_clobbering(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_is_wsl", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"github": {"command": "gh"}}, "other": 1}))
    msg = m.install("cursor", write=True)
    assert str(path) in msg
    data = json.loads(path.read_text())
    assert data["other"] == 1 and data["mcpServers"]["github"] == {"command": "gh"}
    assert data["mcpServers"]["tacit"]["args"] == ["-m", "tacit", "mcp"]
    assert json.loads((tmp_path / ".cursor/mcp.json.bak").read_text())["other"] == 1

    path.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        m.install("cursor", write=True)
    assert path.read_text() == "{not json"
