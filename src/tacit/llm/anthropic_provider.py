import json
from typing import Any

import anthropic
from pydantic import BaseModel

from .base import ChatMessage, Tier, Usage
from .pricing import cost_usd


class AnthropicProvider:
    """Structured outputs via `messages.parse(output_format=...)`; the SDK adapts the
    Pydantic schema to what the API accepts. Low effort: extraction is routine work."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, small_model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key or None, max_retries=3)
        self._models = {"default": model, "small": small_model}

    def model_for(self, tier: Tier) -> str:
        return self._models[tier]

    async def complete_structured(
        self,
        messages: list[ChatMessage],
        schema: type[BaseModel] | None,
        *,
        tier: Tier = "default",
        max_tokens: int = 8192,
    ) -> tuple[dict[str, Any], Usage]:
        model = self.model_for(tier)
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        convo = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": convo,
            "output_config": {"effort": "low"},
        }
        if system:
            kwargs["system"] = system
        if schema is not None:
            resp = await self._client.messages.parse(output_format=schema, **kwargs)
            if resp.stop_reason == "refusal" or resp.parsed_output is None:
                raise RuntimeError(f"No structured output (stop_reason={resp.stop_reason})")
            data = resp.parsed_output.model_dump(mode="json")
        else:
            resp = await self._client.messages.create(**kwargs)
            text = next((b.text for b in resp.content if b.type == "text"), "{}")
            data = json.loads(text)
        u = resp.usage
        cached = (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
        total_in = u.input_tokens + cached
        usage = Usage(
            provider=self.name,
            model=model,
            input_tokens=total_in,
            cached_input_tokens=u.cache_read_input_tokens or 0,
            output_tokens=u.output_tokens,
            cost_usd=cost_usd(model, total_in, u.cache_read_input_tokens or 0, u.output_tokens),
        )
        return data, usage
