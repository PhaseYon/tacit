"""The single place a Graphiti instance is built, and the extraction step of sync."""

import os
from datetime import UTC, datetime
from decimal import Decimal

os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

from graphiti_core import Graphiti  # noqa: E402
from graphiti_core.driver.neo4j_driver import Neo4jDriver  # noqa: E402

from .. import secrets  # noqa: E402
from ..config import Config, get_config  # noqa: E402
from ..index.embeddings import get_embedder  # noqa: E402
from ..llm import LLMProvider  # noqa: E402
from ..sources.base import Item  # noqa: E402
from ..store import (
    Store,  # noqa: E402
    people,  # noqa: E402
)
from ..store import docs as d  # noqa: E402
from .adapters import NoRerank, TacitEmbedder, TacitGraphitiLLMClient  # noqa: E402
from .costs import BudgetExceeded, CostContext, current_cost  # noqa: E402
from .ontology import (  # noqa: E402
    EDGE_TYPE_MAP,
    EDGE_TYPES,
    ENTITY_TYPES,
    EXTRACTION_INSTRUCTIONS,
)

# One group for the whole team graph (design §4): cross-source people and decisions can
# only merge inside a group, and Neo4j full-text breaks past ~128 groups.
GROUP = "tacit"


class _Driver(Neo4jDriver):
    """Graphiti's driver schedules index creation on its own whenever it is built inside a
    running event loop: an unlogged schema write on every search. Here schema changes only
    happen when Extractor.setup() asks for them (and logs it)."""

    allow_schema = False

    async def build_indices_and_constraints(self, delete_existing: bool = False):
        if not self.allow_schema:
            return None
        return await super().build_indices_and_constraints(delete_existing)


class _NoLLM:
    """Read paths never need an LLM; this makes any accidental call fail loudly."""

    name = "none"

    def model_for(self, tier: str) -> str:
        return "none"

    async def complete_structured(self, *a, **kw):
        raise RuntimeError("LLM calls are not allowed on the read path")


def build_graphiti(provider: LLMProvider | None = None, config: Config | None = None) -> Graphiti:
    """provider=None gives a read-only instance (search only)."""
    c = config or get_config()
    password = secrets.require("neo4j_password", "Run `tacit init` or set NEO4J_PASSWORD.")
    driver = _Driver(c.graph.uri, c.graph.user, password, database=c.graph.database)
    return Graphiti(
        graph_driver=driver,
        llm_client=TacitGraphitiLLMClient(provider or _NoLLM()),  # type: ignore[arg-type]
        embedder=TacitEmbedder(get_embedder()),
        cross_encoder=NoRerank(),
        max_coroutines=4,
    )


class Extractor:
    """Turns Items into Graphiti episodes, with a hard per-run budget."""

    def __init__(self, store: Store, provider: LLMProvider, max_usd: float) -> None:
        self.store = store
        self.provider = provider
        self.graphiti = build_graphiti(provider)
        self.budget = Decimal(str(max_usd))
        self.spent = Decimal(0)

    async def setup(self) -> None:
        async with self.store.access("write", "schema.graphiti"):
            self.graphiti.driver.allow_schema = True
            try:
                await self.graphiti.build_indices_and_constraints()
            finally:
                self.graphiti.driver.allow_schema = False

    async def close(self) -> None:
        await self.graphiti.close()

    async def extract(self, item: Item) -> str:
        """Replace the item's episode. Raises BudgetExceeded before spending past the cap."""
        remaining = self.budget - self.spent
        if remaining <= 0:
            raise BudgetExceeded(f"LLM budget for this run reached (${self.budget})")
        old = await d.episode_of(self.store, item.external_id)
        header = await people.identity_header(self.store, list(item.participants), item.metadata)
        body = f"{header}\n\n{item.text}" if header else item.text
        ctx = CostContext(remaining)
        token = current_cost.set(ctx)
        try:
            async with self.store.access(
                "write", "extract", {"external_id": item.external_id, "url": item.url}
            ) as outcome:
                try:
                    if old:
                        await self.graphiti.remove_episode(old)
                        await d.set_episode(self.store, item.external_id, None, None)
                    result = await self.graphiti.add_episode(
                        name=item.title[:250] or item.external_id,
                        episode_body=body,
                        source_description=f"{item.source} {item.kind} {item.url}",
                        reference_time=item.reference_time or datetime.now(UTC),
                        group_id=GROUP,
                        entity_types=ENTITY_TYPES,
                        edge_types=EDGE_TYPES,
                        edge_type_map=EDGE_TYPE_MAP,
                        custom_extraction_instructions=EXTRACTION_INSTRUCTIONS,
                    )
                finally:
                    outcome["cost_usd"] = float(ctx.spent)
                    self.spent += ctx.spent
        finally:
            current_cost.reset(token)
        await d.set_episode(self.store, item.external_id, result.episode.uuid, item.content_hash)
        return result.episode.uuid
