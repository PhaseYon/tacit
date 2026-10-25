"""The single place a Graphiti instance is built."""

from graphiti_core import Graphiti

from ..config import get_settings
from ..llm import LLMProvider, get_provider
from ..search.embeddings import get_embedder
from .adapters import NoRerank, TacitEmbedder, TacitGraphitiLLMClient


def build_graphiti(provider: LLMProvider | None = None) -> Graphiti:
    s = get_settings()
    return Graphiti(
        s.neo4j_uri,
        s.neo4j_user,
        s.neo4j_password.get_secret_value(),
        llm_client=TacitGraphitiLLMClient(provider or get_provider()),
        embedder=TacitEmbedder(get_embedder()),
        cross_encoder=NoRerank(),
        max_coroutines=4,
    )


def group_for(container_id: str) -> str:
    """One Graphiti group per source container (design §3.1)."""
    return f"c-{container_id}"
