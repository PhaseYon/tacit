"""The single place a Graphiti instance is built."""

from graphiti_core import Graphiti

from ..config import get_settings
from ..llm import LLMProvider, get_provider
from ..search.embeddings import get_embedder
from .adapters import NoRerank, TacitEmbedder, TacitGraphitiLLMClient


class _NoLLM:
    """Read paths never need an LLM; this makes any accidental call fail loudly."""

    name = "none"

    def model_for(self, tier: str) -> str:
        return "none"

    async def complete_structured(self, *a, **kw):
        raise RuntimeError("LLM calls are not allowed on the graph read path")


def build_graphiti(provider: LLMProvider | None = None, *, read_only: bool = False) -> Graphiti:
    s = get_settings()
    llm = _NoLLM() if read_only else (provider or get_provider())
    return Graphiti(
        s.neo4j_uri,
        s.neo4j_user,
        s.neo4j_password.get_secret_value(),
        llm_client=TacitGraphitiLLMClient(llm),  # type: ignore[arg-type]
        embedder=TacitEmbedder(get_embedder()),
        cross_encoder=NoRerank(),
        max_coroutines=4,
    )


def group_for(container_id: str) -> str:
    """One Graphiti group per source container (design §3.1)."""
    return f"c-{container_id}"


_reader: Graphiti | None = None


def graph_reader() -> Graphiti | None:
    """Shared read-only instance for the API/MCP, or None when the graph is disabled."""
    global _reader
    if not get_settings().graph_enabled:
        return None
    if _reader is None:
        _reader = build_graphiti(read_only=True)
    return _reader
