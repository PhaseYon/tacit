"""One identity per person across tools (design §5.2).

A :TacitPerson owns handles like "github:dana-gh", "slack:U0123", "notion:Dana Lee" (the
same strings sources put in Item.participants). A handle belongs to at most one person.
Linking is manual (`tacit people link`) or by shared email (`tacit people auto`, using
`metadata["emails"]` that sources provide when the API exposes them).

Extraction prepends a short "known people" header to each episode, so Graphiti resolves
"@dana-gh" (GitHub) and "@dana" (Slack) to one Person entity.
"""

import json
import re
from collections import defaultdict
from typing import Any

from . import Store

SOURCE_NAMES = {"github": "GitHub", "slack": "Slack", "notion": "Notion"}


def person_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "person"


async def link(store: Store, name: str, handles: list[str]) -> str:
    """Attach handles to the person called `name` (created if new), moving them away from
    anyone else who had them. Returns the person key."""
    for h in handles:
        if ":" not in h:
            raise ValueError(
                f"Handles look like github:login, slack:U0123, notion:Name (got {h!r})"
            )
    key = person_key(name)
    await store.run_query(
        "write",
        "people.link",
        "OPTIONAL MATCH (other:TacitPerson) WHERE other.key <> $key"
        " FOREACH (_ IN CASE WHEN other IS NULL THEN [] ELSE [1] END |"
        "   SET other.handles = [h IN other.handles WHERE NOT h IN $handles])"
        " WITH count(*) AS _"
        " MERGE (p:TacitPerson {key: $key})"
        " SET p.name = $name,"
        "     p.handles = coalesce(p.handles, []) + [h IN $handles WHERE NOT h IN coalesce(p.handles, [])],"
        "     p.updated_at = datetime(), p.updated_by = $who",
        {"key": key, "name": name, "handles": handles, "who": store.who},
    )
    await store.run_query(
        "write",
        "people.link",
        "MATCH (p:TacitPerson) WHERE size(p.handles) = 0 DELETE p",
    )
    return key


async def unlink(store: Store, handle: str) -> bool:
    rows = await store.run_query(
        "write",
        "people.unlink",
        "MATCH (p:TacitPerson) WHERE $h IN p.handles"
        " SET p.handles = [x IN p.handles WHERE x <> $h] RETURN count(p) AS n",
        {"h": handle},
    )
    await store.run_query(
        "write", "people.unlink", "MATCH (p:TacitPerson) WHERE size(p.handles) = 0 DELETE p"
    )
    return bool(rows and rows[0]["n"])


async def people(store: Store) -> list[dict[str, Any]]:
    return await store.run_query(
        "read",
        "people.list",
        "MATCH (p:TacitPerson) RETURN p.key AS key, p.name AS name, p.handles AS handles"
        " ORDER BY p.name",
    )


async def _seen(store: Store) -> list[dict[str, Any]]:
    """Every participant handle on any doc, with doc counts and the metadata that names it."""
    return await store.run_query(
        "read",
        "people.seen",
        "MATCH (d:TacitDoc) UNWIND d.participants AS h"
        " WITH h, count(d) AS docs, collect(d.metadata)[0..5] AS metas"
        " RETURN h, docs, metas ORDER BY docs DESC, h",
    )


def _from_metadata(metas: list[str], handle: str, field: str) -> str | None:
    for raw in metas:
        value = (json.loads(raw or "{}").get(field) or {}).get(handle)
        if value:
            return value
    return None


async def unlinked(store: Store, limit: int = 50) -> list[dict[str, Any]]:
    linked = {h for p in await people(store) for h in p["handles"] or []}
    out = []
    for row in await _seen(store):
        if row["h"] in linked:
            continue
        out.append(
            {
                "handle": row["h"],
                "docs": row["docs"],
                "display": _from_metadata(row["metas"], row["h"], "handles"),
                "email": _from_metadata(row["metas"], row["h"], "emails"),
            }
        )
        if len(out) >= limit:
            break
    return out


async def auto_link(store: Store) -> list[tuple[str, list[str]]]:
    """Link handles that share an email address. Returns what was linked."""
    by_email: dict[str, set[str]] = defaultdict(set)
    for row in await _seen(store):
        email = _from_metadata(row["metas"], row["h"], "emails")
        if email:
            by_email[email.lower()].add(row["h"])
    existing = {h: p["name"] for p in await people(store) for h in p["handles"] or []}
    done = []
    for email, handles in sorted(by_email.items()):
        names = {existing[h] for h in handles if h in existing}
        name = sorted(names)[0] if names else email.split("@")[0]
        new = sorted(h for h in handles if h not in existing or existing[h] != name)
        if new:
            await link(store, name, new)
            done.append((name, new))
    return done


def _label(handle: str, display: str | None) -> str:
    source, _, value = handle.partition(":")
    shown = display or value
    return f"@{shown} on {SOURCE_NAMES.get(source, source)}"


async def identity_header(store: Store, participants: list[str], metadata: dict) -> str:
    """Lines telling the extractor which handles are the same person ('' if none)."""
    if not participants:
        return ""
    rows = await store.run_query(
        "read",
        "people.resolve",
        "MATCH (p:TacitPerson) WHERE any(h IN p.handles WHERE h IN $hs)"
        " RETURN p.name AS name, p.handles AS handles ORDER BY p.name",
        {"hs": participants},
    )
    displays = metadata.get("handles") or {}
    lines = []
    for r in rows:
        if len(r["handles"]) < 2:
            continue
        aliases = ", ".join(_label(h, displays.get(h)) for h in r["handles"])
        lines.append(f"- {r['name']}: {aliases}")
    if not lines:
        return ""
    return "Known people (each line is one person across tools):\n" + "\n".join(lines)
