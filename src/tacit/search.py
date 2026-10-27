"""Deterministic hybrid search (design §8). NOT YET IMPLEMENTED: see the search+MCP task.

Contract used by the CLI, MCP server and website:

    await search(store, query, *, limit=10, as_of=None, tool="search") -> dict
    format_results(results: dict) -> str
"""

from typing import Any

from .store import Store


async def search(
    store: Store, query: str, *, limit: int = 10, as_of: str | None = None, tool: str = "search"
) -> dict[str, Any]:
    raise NotImplementedError("search is being built")


def format_results(results: dict[str, Any]) -> str:
    raise NotImplementedError("search is being built")
