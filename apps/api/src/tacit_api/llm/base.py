"""Provider-neutral LLM interface used by the graph layer (and later the agent)."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel

Tier = Literal["small", "default"]


@dataclass(frozen=True)
class Usage:
    provider: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


class LLMProvider(Protocol):
    name: str

    def model_for(self, tier: Tier) -> str: ...

    async def complete_structured(
        self,
        messages: list[ChatMessage],
        schema: type[BaseModel] | None,
        *,
        tier: Tier = "default",
        max_tokens: int = 8192,
    ) -> tuple[dict[str, Any], Usage]:
        """Return the model's JSON answer (validated against `schema` when given)."""
        ...


class LLMNotConfigured(RuntimeError):
    pass
