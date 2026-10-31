"""Shared sync state: scope, cursors, the write lease, and the access log (design §6, §7)."""

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any

from ..sources.base import ScopeEntry
from . import Store

log = logging.getLogger(__name__)

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


async def cursor_set(store: Store, key: str, value: str, *, fence: "Lease | None" = None) -> None:
    await fenced_write(
        store,
        "cursor.set",
        "MERGE (c:TacitCursor {key: $key}) SET c.value = $value, c.updated_at = datetime(),"
        " c.updated_by = $who",
        {"key": key, "value": value, "who": store.who},
        fence=fence,
    )


async def cursors(store: Store) -> dict[str, dict[str, Any]]:
    rows = await store.run_query(
        "read",
        "cursor.list",
        "MATCH (c:TacitCursor) RETURN c.key AS key, c.updated_at AS at, c.updated_by AS who",
    )
    return {r["key"]: {"at": r["at"], "who": r["who"]} for r in rows}


# -- the write lease ---------------------------------------------------------------------


class LeaseLost(RuntimeError):
    """This process no longer holds the write lease; stop writing."""


class FencedOut(LeaseLost):
    """A fenced write was refused: the lease's token has moved on (someone else took it)."""


class Lease:
    """One writer per graph at a time (design §4, §6.1). Graphiti needs episodes in a group
    added one at a time, in order; the lease also stops two laptops paying for the same
    extraction. `SET l._lock` takes Neo4j's write lock before the holder is read, so two
    concurrent claims serialize instead of both succeeding.

    Each new holder gets a higher `token` (a fencing token). Writes made under the lease
    pass it along (`fenced_write`), so a holder that froze past its TTL and wakes up after
    someone else took over is refused instead of writing. While held, `start_renewal()`
    keeps the lease alive in the background every `renew_every` seconds."""

    NAME = "writer"

    def __init__(
        self, store: Store, ttl_seconds: float = 600, renew_every: float | None = None
    ) -> None:
        self.store = store
        self.ttl = ttl_seconds
        self.renew_every = renew_every if renew_every is not None else ttl_seconds / 5
        self.holder = f"{store.who}:{store.client}:{uuid.uuid4().hex[:8]}"
        self.token: int | None = None
        self.lost = False
        self._renewal: asyncio.Task | None = None

    async def claim(self) -> tuple[bool, str | None]:
        """Returns (got_it, current_holder_label). On success `self.token` is the fencing
        token: unchanged if we already held the lease, one higher if we're a new holder."""
        rows = await self.store.run_query(
            "write",
            "lease.claim",
            "MERGE (l:TacitLease {name: $name}) SET l._lock = true WITH l"
            " WITH l, (l.holder IS NULL OR l.holder = $holder OR l.expires_at < datetime()) AS free,"
            "      (l.holder = $holder AND l.token IS NOT NULL) AS mine"
            " FOREACH (_ IN CASE WHEN free THEN [1] ELSE [] END |"
            "   SET l.holder = $holder, l.label = $label,"
            "       l.expires_at = datetime() + duration({seconds: $ttl}),"
            "       l.token = CASE WHEN mine THEN l.token ELSE coalesce(l.token, 0) + 1 END)"
            " REMOVE l._lock RETURN free, l.label AS label, l.token AS token",
            {"name": self.NAME, "holder": self.holder, "label": self.store.who, "ttl": self.ttl},
        )
        row = rows[0]
        got = bool(row["free"])
        if got:
            self.token = row["token"]
            self.lost = False
        return got, row["label"]

    async def renew(self) -> bool:
        rows = await self.store.run_query(
            "write",
            "lease.renew",
            "MATCH (l:TacitLease {name: $name, holder: $holder})"
            " SET l.expires_at = datetime() + duration({seconds: $ttl}) RETURN count(l) AS n",
            {"name": self.NAME, "holder": self.holder, "ttl": self.ttl},
        )
        return bool(rows and rows[0]["n"])

    async def ensure(self, what: str = "sync") -> None:
        """Step-level check: fail fast if the background renewal saw the lease go, else
        renew now. Raises LeaseLost."""
        if self.lost:
            raise LeaseLost(f"Lost the write lease mid-{what} (renewal failed); stopping.")
        if not await self.renew():
            self.lost = True
            raise LeaseLost(f"Lost the write lease mid-{what} (expired?); stopping.")

    def start_renewal(self) -> None:
        if self._renewal is None:
            self._renewal = asyncio.create_task(self._renew_loop(), name="tacit-lease-renewal")

    async def stop_renewal(self) -> None:
        task, self._renewal = self._renewal, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(self.renew_every)
            try:
                ok = await self.renew()
            except Exception:
                log.warning("Renewing the write lease failed", exc_info=True)
                ok = False
            if not ok:
                self.lost = True
                log.warning("Lost the write lease; the sync will stop at its next step.")
                return

    async def release(self) -> None:
        await self.store.run_query(
            "write",
            "lease.release",
            "MATCH (l:TacitLease {name: $name, holder: $holder})"
            " REMOVE l.holder, l.label, l.expires_at",
            {"name": self.NAME, "holder": self.holder},
        )


async def fenced_write(
    store: Store,
    tool: str,
    cypher: str,
    params: dict[str, Any],
    *,
    fence: Lease | None = None,
    log_params: dict[str, Any] | None = None,
) -> None:
    """Run a write, fenced by the lease's token when `fence` is given.

    The token check and the write are one statement (one transaction). It first takes
    the write lock on the lease node (`SET l._fence`), then compares tokens, so a
    concurrent `claim()` either commits before (we see the new token and write nothing)
    or waits until we commit. `cypher` must be a write without RETURN; its parameters
    must not start with `_fence`. Raises FencedOut if the token no longer matches."""
    if fence is None:
        await store.run_query("write", tool, cypher, params, log_params=log_params)
        return
    if fence.token is None:
        raise FencedOut("Fenced write without a claimed lease.")
    rows = await store.run_query(
        "write",
        tool,
        "MATCH (l:TacitLease {name: $_fence_name}) SET l._fence = true"
        " WITH l, coalesce(l.token = $_fence_token, false) AS ok REMOVE l._fence"
        " WITH ok WHERE ok CALL () { " + cypher + " } RETURN ok",
        {**params, "_fence_name": fence.NAME, "_fence_token": fence.token},
        log_params={**(log_params if log_params is not None else params), "fence": fence.token},
    )
    if not rows:
        fence.lost = True
        raise FencedOut(
            f"Write refused: the write lease moved on from token {fence.token} "
            "(someone else holds it now); stopping."
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
