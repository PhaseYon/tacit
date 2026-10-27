"""Local MCP server over stdio (design §8.1), plus `tacit mcp install`.

The AI client (Claude Desktop, Cursor, Claude Code) launches `python -m tacit mcp` and
talks to it over stdin/stdout. Tools build fixed Cypher from their parameters; the model
never writes queries, and every read lands in the team's access log (design §7).

No `ask` tool: the client is already the LLM. It calls `search`, reads the evidence and
writes the answer itself.
"""

import json
import os
import platform
import shlex
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from glob import glob
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .config import get_config
from .search import iso_time
from .search import search as do_search
from .store import Store
from .store import docs as d

SERVER_NAME = "tacit"
MAX_DOC_CHARS = 100_000

INSTRUCTIONS = """\
Tacit is this team's memory: its GitHub PRs/issues/reviews, Slack threads and Notion pages,
plus decisions extracted from them, in a shared knowledge graph. Use it to answer "why did
we do X, who decided, and where is the evidence?".

- Call `search` first. Answer only from what it returns; if it finds nothing relevant, say so.
- Always cite the `url` of every source or fact citation you rely on.
- Facts marked "superseded" were true once and were later replaced; say what replaced them
  and when. Prefer "current" facts for questions about the present.
- Results are a point-in-time snapshot of the last sync. Call `status` when freshness
  matters and mention how old the data is.
- Use `get_document` to read a cited source in full before quoting it at length.
- Every call is recorded in the team's access log under the user's identity.
"""


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """One Store (one Neo4j driver) for the server's lifetime."""
    store = Store.from_config()
    try:
        yield {"store": store}
    finally:
        await store.close()


def _store(ctx: Context) -> Store:
    return ctx.lifespan_context["store"]


_STATUS_QUERY = """
CALL () {
  MATCH (d:TacitDoc)
  WITH d.source AS source, count(*) AS items, max(d.updated_at) AS last_indexed,
       max(d.reference_time) AS newest_item
  RETURN collect({source: source, items: items, last_indexed: last_indexed,
                  newest_item: newest_item}) AS sources
}
CALL () {
  MATCH (s:TacitScope)
  OPTIONAL MATCH (c:TacitCursor {key: s.key})
  RETURN collect({key: s.key, source: s.source, label: s.label, last_sync: c.updated_at,
                  last_sync_by: c.updated_by}) AS scopes
}
CALL () {
  MATCH (d:TacitDoc) WHERE coalesce(d.extract, true)
    AND (d.extracted_hash IS NULL OR d.extracted_hash <> d.content_hash)
  RETURN count(d) AS pending
}
CALL () {
  MATCH ()-[r:RELATES_TO]->()
  RETURN count(r) AS facts,
         count(CASE WHEN r.invalid_at IS NULL AND r.expired_at IS NULL THEN 1 END) AS current
}
RETURN sources, scopes, pending, facts, current
"""


async def get_status(store: Store, *, tool: str = "mcp.status") -> dict[str, Any]:
    rows = await store.run_query("read", tool, _STATUS_QUERY)
    row = rows[0]
    graph = get_config().graph
    sources = sorted(row["sources"], key=lambda s: s["source"] or "")
    scopes = sorted(row["scopes"], key=lambda s: s["key"])
    return {
        "graph": {"host": urlsplit(graph.uri).hostname, "database": graph.database},
        "now": iso_time(datetime.now(UTC)),
        "items": sum(s["items"] for s in sources),
        "sources": [
            {
                **s,
                "last_indexed": iso_time(s["last_indexed"]),
                "newest_item": iso_time(s["newest_item"]),
            }
            for s in sources
        ],
        "scopes": [{**s, "last_sync": iso_time(s["last_sync"])} for s in scopes],
        "facts": {"total": row["facts"], "current": row["current"]},
        "pending_extraction": row["pending"],
    }


def build_server() -> FastMCP:
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, lifespan=_lifespan)

    @mcp.tool
    async def search(
        query: Annotated[
            str, Field(description="A question or keywords, e.g. 'why did we move to Redis?'")
        ],
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=50, description="Max sources and facts")] = 10,
        as_of: Annotated[
            str | None,
            Field(description="ISO date (2026-03-01): only facts that were valid then"),
        ] = None,
    ) -> dict[str, Any]:
        """Search the team's memory (GitHub, Slack, Notion) for evidence and decisions.

        Returns `results` (one per source document: title, url, date, best passages) and,
        once extraction has run, `facts` (decisions/relationships with valid_at /
        invalid_at, status "current" or "superseded", and citation urls). Answer only from
        these results and cite the `url` of every source you use. Results reflect the last
        sync (see `status`). This call is recorded in the team's access log.
        """
        try:
            return await do_search(_store(ctx), query, limit=limit, as_of=as_of, tool="mcp.search")
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def get_document(
        url_or_id: Annotated[
            str, Field(description="A `url` or `external_id` from search results")
        ],
        ctx: Context,
    ) -> dict[str, Any]:
        """Full stored text of one cited source (PR, issue, Slack thread, Notion page) with
        its link, date and participants. Use it to read the evidence behind a search hit
        before quoting it; cite its `url`. This call is recorded in the access log."""
        doc = await d.get_doc(_store(ctx), url_or_id, tool="mcp.get_document")
        if doc is None:
            raise ToolError(f"No stored document for {url_or_id!r}. Use a url from `search`.")
        for key in ("reference_time", "updated_at"):
            doc[key] = iso_time(doc.get(key))
        text = doc.get("text") or ""
        doc["truncated"] = len(text) > MAX_DOC_CHARS
        doc["text"] = text[:MAX_DOC_CHARS]
        return doc

    @mcp.tool
    async def status(ctx: Context) -> dict[str, Any]:
        """How fresh and complete the team graph is: graph host, items per source, last
        sync per scope, fact counts and items still waiting for extraction. Use it to tell
        the user how current an answer is. This call is recorded in the access log."""
        return await get_status(_store(ctx))

    return mcp


def run() -> None:
    build_server().run(transport="stdio", show_banner=False)


# -- install -----------------------------------------------------------------------------

CLIENTS = ("claude", "cursor", "claude-code")


def _is_wsl() -> bool:
    return "microsoft" in platform.uname().release.lower()


def launch_command() -> tuple[str, list[str]]:
    """Works however tacit was installed (uv tool, pipx, venv): this exact interpreter."""
    return sys.executable, ["-m", "tacit", "mcp"]


def _server_entry(*, via_wsl: bool = False) -> dict[str, Any]:
    command, args = launch_command()
    if via_wsl:  # a Windows app launching tacit inside WSL
        command, args = "wsl.exe", ["-e", command, *args]
    entry: dict[str, Any] = {"command": command, "args": args}
    if env := os.environ.get("TACIT_CONFIG_DIR"):
        entry["env"] = {"TACIT_CONFIG_DIR": env}
    return entry


def claude_desktop_config_path() -> Path | None:
    if _is_wsl():
        found = glob("/mnt/c/Users/*/AppData/Roaming/Claude/claude_desktop_config.json")
        found = found or glob("/mnt/c/Users/*/AppData/Roaming/Claude")
        if len(found) == 1:
            p = Path(found[0])
            return p if p.suffix == ".json" else p / "claude_desktop_config.json"
        return None
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def cursor_config_path() -> Path:
    return Path.home() / ".cursor/mcp.json"


def merge_config(path: Path, entry: dict[str, Any]) -> Path:
    """Add/replace only mcpServers.tacit; other servers and settings stay untouched. The
    previous file is kept next to it as .bak."""
    current: dict[str, Any] = {}
    if path.exists():
        raw = path.read_text() or "{}"
        try:
            current = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON; fix it or edit it by hand.") from exc
        if not isinstance(current, dict):
            raise ValueError(f"{path} doesn't contain a JSON object; edit it by hand.")
        path.with_name(path.name + ".bak").write_text(raw)
    servers = current.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"`mcpServers` in {path} isn't an object; edit it by hand.")
    servers[SERVER_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2) + "\n")
    return path


def install(client: str, *, write: bool = False) -> str:
    """Instructions for `client`; with write=True, merge the config into its file."""
    if client not in CLIENTS:
        raise ValueError(f"Unknown client {client!r}; use one of: {', '.join(CLIENTS)}.")

    if client == "claude-code":
        command, args = launch_command()
        env = os.environ.get("TACIT_CONFIG_DIR")
        env_flag = f" -e TACIT_CONFIG_DIR={shlex.quote(env)}" if env else ""
        cmd = f"claude mcp add --scope user{env_flag} {SERVER_NAME} -- " + shlex.join(
            [command, *args]
        )
        return f"Run this to add Tacit to Claude Code (all projects):\n\n  {cmd}\n"

    wsl = _is_wsl() and client == "claude"
    entry = _server_entry(via_wsl=wsl)
    snippet = json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2)
    path = claude_desktop_config_path() if client == "claude" else cursor_config_path()
    app = "Claude Desktop" if client == "claude" else "Cursor"
    where = str(path) if path else r"%APPDATA%\Claude\claude_desktop_config.json (Windows)"

    if write:
        if path is None:
            raise ValueError(f"Couldn't find {app}'s config file; add this to {where}:\n{snippet}")
        merge_config(path, entry)
        return f"Added Tacit to {path}. Restart {app} to load it."
    note = ""
    if wsl:
        note = "\n(Claude Desktop runs on Windows, so it launches Tacit inside WSL via wsl.exe.)\n"
    return (
        f"Add this to {where} (merge into any existing mcpServers),\n"
        f"or rerun with --write to do it for you. Then restart {app}.\n{note}\n{snippet}\n"
    )
