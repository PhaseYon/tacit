"""Small helpers over githubkit's raw requests. We work with plain JSON dicts so the
same normalizers handle REST responses and webhook payloads."""

from typing import Any

from githubkit import GitHub


async def get_json(gh: GitHub, url: str, params: dict[str, Any] | None = None) -> Any:
    return (await gh.arequest("GET", url, params=params)).json()


async def get_page(
    gh: GitHub, url: str, params: dict[str, Any] | None = None
) -> tuple[Any, str | None]:
    """One page plus the next page's full URL (from the Link header), for checkpointing."""
    resp = await gh.arequest("GET", url, params=params)
    nxt = resp.raw_response.links.get("next", {}).get("url")
    return resp.json(), nxt


async def get_all(
    gh: GitHub, url: str, params: dict[str, Any] | None = None, *, key: str | None = None
) -> list[Any]:
    """Every page. `key` picks the list out of wrapped responses like
    {"total_count": n, "repositories": [...]}."""
    items: list[Any] = []
    next_url: str | None = url
    first = True
    while next_url:
        data, next_url = await get_page(gh, next_url, params if first else None)
        first = False
        items.extend(data[key] if key else data)
    return items
