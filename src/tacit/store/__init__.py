"""Everything Tacit keeps in the team's Neo4j, behind one class (design §5, §7).

Every read and write of Tacit's own data goes through `Store.run_query()`, which writes a
:TacitAccess entry FIRST and refuses to run the query if that write fails. Graphiti calls
(extraction, fact search) go through `Store.access()` so they are logged the same way.
"""

import json
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from neo4j import AsyncDriver, AsyncGraphDatabase

from .. import secrets
from ..config import Config, get_config

Op = Literal["read", "write"]
MAX_LOGGED_PARAM_CHARS = 2000


class AccessLogError(RuntimeError):
    """The access log couldn't be written, so the query was not run."""


def _loggable(params: dict[str, Any] | None) -> str:
    if not params:
        return "{}"
    data = json.dumps(params, default=str, sort_keys=True)
    return data[:MAX_LOGGED_PARAM_CHARS]


class Store:
    def __init__(self, driver: AsyncDriver, *, database: str, who: str) -> None:
        self.driver = driver
        self.database = database
        self.who = who or "unknown"
        self.client = socket.gethostname()

    @classmethod
    def from_config(cls, config: Config | None = None) -> "Store":
        c = config or get_config()
        password = secrets.require("neo4j_password", "Run `tacit init` or set NEO4J_PASSWORD.")
        driver = AsyncGraphDatabase.driver(
            c.graph.uri, auth=(c.graph.user, password), notifications_min_severity="OFF"
        )
        return cls(driver, database=c.graph.database, who=c.identity.email)

    async def close(self) -> None:
        await self.driver.close()

    async def __aenter__(self) -> "Store":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- the only path to the database -------------------------------------------------

    async def _execute(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict]:
        records, _, _ = await self.driver.execute_query(
            cypher, params or {}, database_=self.database
        )
        return [r.data() for r in records]

    async def log_access(
        self, op: Op, tool: str, params: dict[str, Any] | None = None, **extra: Any
    ) -> str:
        entry_id = str(uuid.uuid4())
        try:
            await self._execute(
                "CREATE (a:TacitAccess {id: $id, who: $who, op: $op, tool: $tool,"
                " params: $params, client: $client, at: datetime()}) SET a += $extra",
                {
                    "id": entry_id,
                    "who": self.who,
                    "op": op,
                    "tool": tool,
                    "params": _loggable(params),
                    "client": self.client,
                    "extra": {k: v for k, v in extra.items() if v is not None},
                },
            )
        except Exception as exc:  # fail closed: no log, no query
            raise AccessLogError(f"Could not write the access log: {exc}") from exc
        return entry_id

    async def _finish_entry(self, entry_id: str, **values: Any) -> None:
        try:
            await self._execute(
                "MATCH (a:TacitAccess {id: $id}) SET a += $v", {"id": entry_id, "v": values}
            )
        except Exception:  # the entry already exists; a missing count is acceptable
            pass

    async def run_query(
        self,
        op: Op,
        tool: str,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        log_params: dict[str, Any] | None = None,
    ) -> list[dict]:
        """Log, then run. `log_params` replaces `params` in the log (e.g. to drop vectors)."""
        entry = await self.log_access(op, tool, log_params if log_params is not None else params)
        rows = await self._execute(cypher, params)
        await self._finish_entry(entry, result_count=len(rows))
        return rows

    @asynccontextmanager
    async def access(
        self, op: Op, tool: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """For work that doesn't go through run_query (Graphiti). Log first; the caller
        can put result_count / cost_usd / error into the yielded dict."""
        entry = await self.log_access(op, tool, params)
        outcome: dict[str, Any] = {}
        try:
            yield outcome
        except Exception as exc:
            outcome.setdefault("error", repr(exc)[:500])
            raise
        finally:
            if outcome:
                await self._finish_entry(entry, **outcome)
