"""The only read path into the graph (design §3.3). Runs inside the caller's tacit_api
RLS session. Fails closed at every step:

1. Allowed groups come from Postgres (app.readable_container_ids()).
2. No allowed groups -> return [] WITHOUT calling Graphiti ([] there means "all groups").
3. Search in batches of <= 100 groups (Neo4j full-text query length limit).
4. Drop any result whose group isn't allowed.
5. Keep a fact only if one of its episodes maps to a visible, finished graph_episode row
   (same RESTRICTIVE ACL as documents) — that row also supplies the citation.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .client import group_for

log = logging.getLogger(__name__)
MAX_GROUPS_PER_QUERY = 100


@dataclass(frozen=True)
class Citation:
    url: str
    title: str | None
    kind: str
    container: str


@dataclass
class Fact:
    fact: str
    relation: str
    valid_at: datetime | None
    invalid_at: datetime | None
    status: str  # current | superseded
    citations: list[Citation] = field(default_factory=list)


async def allowed_groups(session: AsyncSession) -> list[str]:
    ids = (await session.execute(text("SELECT app.readable_container_ids()"))).scalars()
    return [group_for(str(i)) for i in ids]


async def search_facts(
    session: AsyncSession, graphiti, query: str, *, include_history: bool = False, limit: int = 10
) -> list[Fact]:
    from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
    from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters

    groups = await allowed_groups(session)
    if not groups:
        return []
    allowed = set(groups)

    search_filter = None
    if not include_history:
        nulls = [[DateFilter(comparison_operator=ComparisonOperator.is_null)]]
        search_filter = SearchFilters(invalid_at=nulls, expired_at=nulls)
    config = EDGE_HYBRID_SEARCH_RRF.model_copy(update={"limit": limit})

    edges = []
    for i in range(0, len(groups), MAX_GROUPS_PER_QUERY):
        batch = groups[i : i + MAX_GROUPS_PER_QUERY]
        assert batch, "never call Graphiti with an empty group list"
        results = await graphiti.search_(
            query, config=config, group_ids=batch, search_filter=search_filter
        )
        edges.extend(results.edges)

    safe = []
    for e in edges:
        if e.group_id not in allowed:
            log.error("Graph returned edge %s from non-allowed group %s", e.uuid, e.group_id)
            continue
        safe.append(e)
    if not safe:
        return []

    episode_uuids = sorted({u for e in safe for u in (e.episodes or [])})
    rows = await session.execute(
        text(
            "SELECT g.episode_uuid, d.url, d.title, d.kind, c.name FROM graph_episode g"
            " JOIN source_document d ON d.id = g.root_document_id"
            " JOIN source_container c ON c.id = g.container_id"
            " WHERE g.status = 'done' AND g.episode_uuid = ANY(:uuids) AND d.deleted_at IS NULL"
        ),
        {"uuids": episode_uuids},
    )
    cites = {r.episode_uuid: Citation(r.url, r.title, r.kind, r.name) for r in rows}

    facts: list[Fact] = []
    for e in safe:
        citations = [cites[u] for u in (e.episodes or []) if u in cites]
        if not citations:
            continue  # Postgres doesn't vouch for any source of this fact: drop it.
        facts.append(
            Fact(
                fact=e.fact,
                relation=e.name,
                valid_at=e.valid_at,
                invalid_at=e.invalid_at,
                status="superseded" if (e.invalid_at or e.expired_at) else "current",
                citations=citations,
            )
        )
    facts.sort(
        key=lambda f: (f.status != "current", -(f.valid_at.timestamp() if f.valid_at else 0))
    )
    return facts[:limit]
