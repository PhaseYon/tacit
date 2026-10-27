"""Shared sync state: scope, cursors, the write lease, and the access log (design §6, §7)."""

import json
import uuid
from typing import Any

from ..sources.base import ScopeEntry
from . import Store

# -- scope -------------------------------------------------------------------------------


async def scope_add(store: Store, entry: ScopeEntry) -> None:
    await store.run_query(
        "write",
        "scope.add",
        "MERGE (s:TacitScope {key: $key}) SET s.source = $source, s.target = $target,"
        " s.label = $label, s.options = $options, s.added_by = $who,"
        " s.added_at = coalesce(s.added_at, datetime())",
        {
            "key": entry.key,
            "source": entry.source,
            "target": entry.target,
            "label": entry.label,
            "options": json.dumps(entry.options, sort_keys=True),
            "who": store.who,
        },
    )


async def scope_list(store: Store) -> list[ScopeEntry]:
    rows = await store.run_query(
        "read",
        "scope.list",
        "MATCH (s:TacitScope) RETURN s.key AS key, s.source AS source, s.target AS target,"
        " s.label AS label, s.options AS options ORDER BY s.key",
    )
    return [
        ScopeEntry(r["key"], r["source"], r["target"], r["label"], json.loads(r["options"] or "{}"))
        for r in rows
    ]


async def scope_remove(store: Store, key: str) -> bool:
    rows = await store.run_query(
        "write",
        "scope.remove",
        "MATCH (s:TacitScope {key: $key}) DETACH DELETE s RETURN count(*) AS n",
        {"key": key},
    )
    return bool(rows and rows[0]["n"])


# -- cursors -----------------------------------------------------------------------------


async def cursor_get(store: Store, key: str) -> str | None:
    rows = await store.run_query(
        "read", "cursor.get", "MATCH (c:TacitCursor {key: $key}) RETURN c.value AS v", {"key": key}
    )
    return rows[0]["v"] if rows else None


async def cursor_set(store: Store, key: str, value: str) -> None:
    await store.run_query(
        "write",
        "cursor.set",
        "MERGE (c:TacitCursor {key: $key}) SET c.value = $value, c.updated_at = datetime(),"
        " c.updated_by = $who",
        {"key": key, "value": value, "who": store.who},
    )


async def cursors(store: Store) -> dict[str, dict[str, Any]]:
    rows = await store.run_query(
        "read",
        "cursor.list",
        "MATCH (c:TacitCursor) RETURN c.key AS key, c.updated_at AS at, c.updated_by AS who",
    )
    return {r["key"]: {"at": r["at"], "who": r["who"]} for r in rows}


# -- the write lease ---------------------------------------------------------------------


class Lease:
    """One writer per graph at a time (design §4, §6.1). Graphiti needs episodes in a group
    added one at a time, in order; the lease also stops two laptops paying for the same
    extraction. `SET l._lock` takes Neo4j's write lock before the holder is read, so two
    concurrent claims serialize instead of both succeeding."""

    NAME = "writer"

    def __init__(self, store: Store, ttl_seconds: int = 600) -> None:
        self.store = store
        self.ttl = ttl_seconds
        self.holder = f"{store.who}:{store.client}:{uuid.uuid4().hex[:8]}"

    async def claim(self) -> tuple[bool, str | None]:
        """Returns (got_it, current_holder_label)."""
        rows = await self.store.run_query(
            "write",
            "lease.claim",
            "MERGE (l:TacitLease {name: $name}) SET l._lock = true WITH l"
            " WITH l, (l.holder IS NULL OR l.holder = $holder OR l.expires_at < datetime()) AS free"
            " FOREACH (_ IN CASE WHEN free THEN [1] ELSE [] END |"
            "   SET l.holder = $holder, l.label = $label,"
            "       l.expires_at = datetime() + duration({seconds: $ttl}))"
            " REMOVE l._lock RETURN free, l.label AS label, l.expires_at AS expires_at",
            {"name": self.NAME, "holder": self.holder, "label": self.store.who, "ttl": self.ttl},
        )
        row = rows[0]
        return bool(row["free"]), row["label"]

    async def renew(self) -> bool:
        rows = await self.store.run_query(
            "write",
            "lease.renew",
            "MATCH (l:TacitLease {name: $name, holder: $holder})"
            " SET l.expires_at = datetime() + duration({seconds: $ttl}) RETURN count(l) AS n",
            {"name": self.NAME, "holder": self.holder, "ttl": self.ttl},
        )
        return bool(rows and rows[0]["n"])

    async def release(self) -> None:
        await self.store.run_query(
            "write",
            "lease.release",
            "MATCH (l:TacitLease {name: $name, holder: $holder})"
            " REMOVE l.holder, l.label, l.expires_at",
            {"name": self.NAME, "holder": self.holder},
        )


# -- access log --------------------------------------------------------------------------


async def access_log(
    store: Store,
    *,
    who: str | None = None,
    since_days: int | None = None,
    grep: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return await store.run_query(
        "read",
        "log.read",
        "MATCH (a:TacitAccess)"
        " WHERE NOT a.tool STARTS WITH 'log.'"
        "   AND ($who IS NULL OR a.who = $who)"
        "   AND ($days IS NULL OR a.at >= datetime() - duration({days: $days}))"
        "   AND ($grep IS NULL OR toLower(a.params) CONTAINS toLower($grep)"
        "        OR toLower(a.tool) CONTAINS toLower($grep))"
        " RETURN a.at AS at, a.who AS who, a.op AS op, a.tool AS tool, a.params AS params,"
        "        a.result_count AS result_count, a.cost_usd AS cost_usd, a.error AS error,"
        "        a.client AS client"
        " ORDER BY a.at DESC LIMIT $limit",
        {"who": who, "days": since_days, "grep": grep, "limit": limit},
    )


async def prune_access_log(store: Store, keep_days: int) -> int:
    rows = await store.run_query(
        "write",
        "log.prune",
        "MATCH (a:TacitAccess) WHERE a.at < datetime() - duration({days: $days})"
        " WITH a LIMIT 10000 DETACH DELETE a RETURN count(*) AS n",
        {"days": keep_days},
    )
    return rows[0]["n"] if rows else 0
