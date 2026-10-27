"""The `tacit` command (design §9). Thin: parses args, calls library code, prints."""

import asyncio
import json
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import typer

from . import secrets
from .config import config_path, get_config, save_config

app = typer.Typer(
    help="Your team's memory, wired into every AI tool.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
scope_app = typer.Typer(help="What this team graph syncs (the permission boundary).")
app.add_typer(scope_app, name="scope")
mcp_app = typer.Typer(help="The local MCP server for Claude, Cursor and Claude Code.")
app.add_typer(mcp_app, name="mcp")


def _run[T](fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    from .store import AccessLogError
    from .store.schema import SchemaMismatch

    try:
        return asyncio.run(fn(*args, **kwargs))
    except (secrets.SecretMissing, SchemaMismatch, AccessLogError, ValueError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc


async def _with_store[T](fn: Callable[..., Awaitable[T]], *args: Any) -> T:
    from .store import Store

    async with Store.from_config() as store:
        return await fn(store, *args)


def _dump(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


# -- setup -------------------------------------------------------------------------------


@app.command()
def init(
    uri: Annotated[str, typer.Option(prompt="Neo4j URI (e.g. neo4j+s://xxxx.databases.neo4j.io)")],
    email: Annotated[str, typer.Option(prompt="Your email (recorded in the access log)")],
    user: Annotated[str, typer.Option()] = "neo4j",
    database: Annotated[str, typer.Option()] = "neo4j",
    password: Annotated[
        str, typer.Option(prompt="Neo4j password", hide_input=True, envvar="NEO4J_PASSWORD")
    ] = "",
) -> None:
    """Connect to your team's Neo4j and prepare it (safe to re-run)."""
    from .store import Store
    from .store.schema import ensure_schema

    save_config({"graph": {"uri": uri, "user": user, "database": database}})
    save_config({"identity": {"email": email}})
    where = secrets.set("neo4j_password", password)

    async def go() -> dict:
        store = Store.from_config()
        try:
            await store.driver.verify_connectivity()
            from .index.embeddings import get_embedder

            return await ensure_schema(store, get_embedder().model)
        finally:
            await store.close()

    meta = _run(go)
    typer.secho(
        f"Connected. Schema v{meta['version']}, embedder {meta['embedding_model']}.", fg="green"
    )
    typer.echo(f"Config: {config_path()}  ·  password stored in: {where}")
    typer.echo("Next: `tacit auth github`, then `tacit scope add github:<owner>/<repo>`.")


AUTH_SECRETS: dict[str, secrets.Name] = {
    "github": "github_token",
    "slack": "slack_token",
    "notion": "notion_token",
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
}


@app.command()
def auth(
    service: Annotated[str, typer.Argument(help="github | slack | notion | anthropic | openai")],
    token: Annotated[str | None, typer.Option(help="Token; prompted if omitted")] = None,
) -> None:
    """Store a token for a source or LLM provider (keychain, or a 0600 file)."""
    if service not in AUTH_SECRETS:
        typer.secho(f"Unknown service {service!r}", fg="red", err=True)
        raise typer.Exit(1)
    if not token and service == "github" and shutil.which("gh"):
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            token = out.stdout.strip()
            typer.echo("Using your GitHub CLI token (gh auth token).")
    if not token:
        token = typer.prompt(f"{service} token", hide_input=True)
    where = secrets.set(AUTH_SECRETS[service], token.strip())
    typer.secho(f"Saved {service} token to {where}.", fg="green")


# -- scope -------------------------------------------------------------------------------


@scope_app.command("add")
def scope_add(
    target: Annotated[
        str, typer.Argument(help="github:owner/repo | slack:#channel | notion:<page>")
    ],
    private: Annotated[
        bool,
        typer.Option(
            help="Allow a private Slack channel (everyone with the graph password can read it)"
        ),
    ] = False,
) -> None:
    """Add a repo, channel or Notion page tree to the team graph."""
    from .sources.registry import source_for
    from .store import state as st

    if ":" not in target:
        typer.secho("Use <source>:<target>, e.g. github:acme/api", fg="red", err=True)
        raise typer.Exit(1)
    name, rest = target.split(":", 1)

    async def go(store):
        source = source_for(name)
        entry = await source.resolve(rest)
        if entry.options.get("private") and not private:
            raise ValueError(
                f"{entry.label} is private. Everyone with this graph's password will be able to"
                " read it. Re-run with --private if that's intended."
            )
        await st.scope_add(store, entry)
        return entry

    from .sources.base import SourceNotConfigured

    try:
        entry = _run(_with_store, go)
    except SourceNotConfigured as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc
    typer.secho(f"Added {entry.key} ({entry.label}). Run `tacit sync`.", fg="green")


@scope_app.command("ls")
def scope_ls(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List what this graph syncs, with last sync time."""
    from .store import state as st

    async def go(store):
        return await st.scope_list(store), await st.cursors(store)

    entries, cursors = _run(_with_store, go)
    if as_json:
        _dump([{**e.__dict__, "last_sync": cursors.get(e.key)} for e in entries])
        return
    if not entries:
        typer.echo("Nothing in scope yet. Add one with `tacit scope add github:owner/repo`.")
    for e in entries:
        c = cursors.get(e.key)
        when = f"last synced {c['at']} by {c['who']}" if c else "never synced"
        typer.echo(f"{e.key:40} {e.label:30} {when}")


@scope_app.command("rm")
def scope_rm(key: str) -> None:
    """Stop syncing a scope entry (already-synced content stays until `tacit forget`)."""
    from .store import state as st

    removed = _run(_with_store, st.scope_remove, key)
    typer.echo("Removed." if removed else f"No scope entry {key!r}.")


# -- sync --------------------------------------------------------------------------------


@app.command()
def sync(
    only: Annotated[list[str] | None, typer.Option(help="Scope key or source name")] = None,
    extract: Annotated[
        bool | None, typer.Option(help="Run Graphiti extraction (uses your LLM key)")
    ] = None,
    max_usd: Annotated[float | None, typer.Option(help="LLM budget for this run")] = None,
    watch: Annotated[int | None, typer.Option(help="Repeat every N minutes")] = None,
) -> None:
    """Pull new and changed items from everything in scope into the team graph."""
    import time

    while True:
        report = _run(_sync_once, only, extract, max_usd)
        _print_report(report)
        if not watch:
            if any(s.error for s in report.scopes):
                raise typer.Exit(1)
            return
        time.sleep(watch * 60)


async def _sync_once(only, extract, max_usd):
    from .sources.registry import available_sources
    from .store import Store
    from .sync import run_sync

    c = get_config()
    do_extract = c.llm.extract if extract is None else extract
    async with Store.from_config() as store:
        extractor = None
        if do_extract:
            from .graph.client import Extractor
            from .llm import get_provider

            extractor = Extractor(store, get_provider(), max_usd or c.llm.max_usd_per_run)
            await extractor.setup()
        try:
            return await run_sync(
                store,
                available_sources(c),
                extractor=extractor,
                only=only,
                progress=lambda m: typer.echo(f"  {m}", err=True),
            )
        finally:
            if extractor:
                await extractor.close()


def _print_report(r) -> None:
    if r.blocked_by:
        typer.secho(
            f"{r.blocked_by} is syncing right now; try again in a few minutes.", fg="yellow"
        )
        return
    for s in r.scopes:
        if s.skipped_reason:
            typer.echo(f"- {s.key}: skipped ({s.skipped_reason})")
        elif s.error:
            typer.secho(f"- {s.key}: error {s.error}", fg="red")
        else:
            typer.echo(f"- {s.key}: {s.written} new/changed, {s.unchanged} unchanged")
    if r.extracted or r.extraction_failed or r.extraction_note:
        typer.echo(
            f"Extraction: {r.extracted} done, {r.extraction_failed} failed, ${r.spent_usd:.4f} spent."
        )
    if r.extraction_note:
        typer.secho(f"  {r.extraction_note}", fg="yellow")
    if r.extraction_pending:
        typer.echo(f"{r.extraction_pending} items not yet extracted into graph facts.")


# -- read --------------------------------------------------------------------------------


@app.command()
def search(
    query: str,
    limit: Annotated[int, typer.Option()] = 10,
    as_of: Annotated[str | None, typer.Option(help="ISO date: facts valid at that time")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Deterministic hybrid search over the team graph (no LLM)."""
    from .search import format_results
    from .search import search as do_search

    results = _run(
        _with_store,
        lambda store: do_search(store, query, limit=limit, as_of=as_of, tool="cli.search"),
    )
    if as_json:
        _dump(results)
    else:
        typer.echo(format_results(results))


@app.command()
def get(url_or_id: str, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show the full stored text of a cited item."""
    from .store import docs as d

    doc = _run(_with_store, lambda store: d.get_doc(store, url_or_id, tool="cli.get"))
    if doc is None:
        typer.secho("Not found.", fg="red", err=True)
        raise typer.Exit(1)
    if as_json:
        _dump(doc)
    else:
        typer.echo(f"{doc['title']}\n{doc['url']}\n\n{doc['text']}")


@app.command()
def log(
    who: Annotated[str | None, typer.Option()] = None,
    since: Annotated[int | None, typer.Option(help="Days")] = 7,
    grep: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option()] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Who read or wrote what in this graph (root-cause analysis)."""
    from .store import state as st

    rows = _run(
        _with_store,
        lambda store: st.access_log(store, who=who, since_days=since, grep=grep, limit=limit),
    )
    if as_json:
        _dump(rows)
        return
    for r in rows:
        extra = f" → {r['result_count']} rows" if r.get("result_count") is not None else ""
        cost = f" ${r['cost_usd']:.4f}" if r.get("cost_usd") else ""
        typer.echo(
            f"{r['at']}  {r['who']:28} {r['op']:5} {r['tool']:16} {r['params'][:80]}{extra}{cost}"
        )


@app.command()
def forget(url_or_id: str) -> None:
    """Delete one item (and its graph episode) from the team graph."""
    from .store import docs as d

    async def go(store):
        episode = await d.delete_doc(store, url_or_id)
        if episode:
            from .graph.client import build_graphiti

            g = build_graphiti()
            try:
                async with store.access("write", "forget.episode", {"episode": episode}):
                    await g.remove_episode(episode)
            finally:
                await g.close()
        return episode

    result = _run(_with_store, go)
    typer.echo("Not found." if result is None else "Forgotten.")


@app.command()
def doctor() -> None:
    """Check config, secrets, the database and source tokens."""
    from .sources.registry import SOURCES

    c = get_config()
    ok = True
    typer.echo(f"Config file:   {config_path()}")
    typer.echo(f"Secret store:  {secrets.backend()}")
    typer.echo(f"Identity:      {c.identity.email or '(not set: run tacit init)'}")
    typer.echo(f"Graph:         {c.graph.uri} (db {c.graph.database})")

    async def check(store):
        from .store import docs as d

        await store.driver.verify_connectivity()
        return await d.stats(store)

    try:
        stats = asyncio.run(_with_store(check))
        total = sum(s["docs"] for s in stats["sources"])
        typer.secho(
            f"Database:      reachable · {total} items · {stats['facts']} facts", fg="green"
        )
    except Exception as exc:  # report, don't crash
        ok = False
        typer.secho(f"Database:      NOT reachable ({exc})", fg="red")
        if "aura" in c.graph.uri.lower() or "neo4j.io" in c.graph.uri:
            typer.echo(
                "               Aura Free pauses after 3 idle days: resume it in the Aura console."
            )
    for name, (_, _, secret, hint) in SOURCES.items():
        have = bool(secrets.get(secret))
        typer.echo(f"{name + ':':15}{'token set' if have else 'no token (' + hint + ')'}")
    llm_key = secrets.get("anthropic_api_key") or secrets.get("openai_api_key")
    typer.echo(
        f"Extraction:    {'on' if c.llm.extract else 'off'}"
        f" ({c.llm.provider}, {'key set' if llm_key else 'no key'})"
    )
    if not ok:
        raise typer.Exit(1)


# -- MCP ---------------------------------------------------------------------------------


@mcp_app.callback(invoke_without_command=True)
def mcp_main(ctx: typer.Context) -> None:
    """Run the MCP server over stdio (what your AI client launches)."""
    if ctx.invoked_subcommand is None:
        from .mcp import run

        run()


@mcp_app.command("install")
def mcp_install(
    client: Annotated[str, typer.Option(help="claude | cursor | claude-code")] = "claude",
    write: Annotated[
        bool, typer.Option("--write", help="Merge it into the client's config file")
    ] = False,
) -> None:
    """Print (or apply) the config that makes your AI client launch Tacit."""
    from .mcp import install

    try:
        typer.echo(install(client, write=write))
    except ValueError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc


def main() -> None:  # pragma: no cover
    sys.exit(app())
