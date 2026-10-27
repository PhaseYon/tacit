"""Adapters that let Graphiti use Tacit's LLM provider and local embeddings."""

import asyncio
from typing import Any

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig, ModelSize
from graphiti_core.prompts.models import Message
from pydantic import BaseModel

from ..index.embeddings import Embedder
from ..llm import ChatMessage, LLMProvider
from .costs import BudgetExceeded, current_cost, current_prompt


class TacitGraphitiLLMClient(LLMClient):
    """Every call goes through our provider, is costed, and checks the budget first.
    A BudgetExceeded aborts the episode before Graphiti persists anything."""

    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(
            LLMConfig(model=provider.model_for("default"), small_model=provider.model_for("small"))
        )
        self.provider = provider

    async def generate_response(self, messages: list[Message], *args: Any, **kwargs: Any):
        token = current_prompt.set(kwargs.get("prompt_name"))
        try:
            return await super().generate_response(messages, *args, **kwargs)
        finally:
            current_prompt.reset(token)

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 16384,
        model_size: ModelSize = ModelSize.medium,
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        ctx = current_cost.get()
        if ctx is None:
            raise BudgetExceeded("LLM call outside a cost context (refusing to spend unattributed)")
        ctx.check()
        tier = "small" if model_size == ModelSize.small else "default"
        data, usage = await self.provider.complete_structured(
            [ChatMessage(role=m.role, content=m.content) for m in messages],  # type: ignore[arg-type]
            response_model,
            tier=tier,
            max_tokens=min(max_tokens, 16000),
        )
        ctx.records.append((current_prompt.get(), usage))
        return data

    async def _generate_response_with_retry(self, *args: Any, **kwargs: Any):
        # Never retry past a budget stop; the SDKs already retry 429/5xx themselves.
        return await self._generate_response(*args, **kwargs)


class TacitEmbedder(EmbedderClient):
    """Reuses the step-3 embedder (local bge-small by default, 384 dims)."""

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder

    async def create(self, input_data) -> list[float]:  # type: ignore[override]
        text = input_data if isinstance(input_data, str) else " ".join(map(str, input_data))
        return (await asyncio.to_thread(self.embedder.embed_documents, [text]))[0]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        if not input_data_list:
            return []
        return await asyncio.to_thread(self.embedder.embed_documents, input_data_list)


class NoRerank(CrossEncoderClient):
    """We only use RRF / node-distance recipes; this stops Graphiti defaulting to OpenAI."""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        raise RuntimeError("Cross-encoder reranking is disabled in Tacit")
