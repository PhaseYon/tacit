import json
from typing import Any

from pydantic import BaseModel

from .base import ChatMessage, Tier, Usage
from .pricing import cost_usd


class OpenAIProvider:
    """Structured outputs via the Responses API (`responses.parse(text_format=...)`)."""

    name = "openai"

    def __init__(self, api_key: str, model: str, small_model: str) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key or None, max_retries=3)
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
        inp = [{"role": m.role, "content": m.content} for m in messages]
        if schema is not None:
            resp = await self._client.responses.parse(
                model=model, input=inp, text_format=schema, max_output_tokens=max_tokens
            )
            if resp.output_parsed is None:
                raise RuntimeError("No structured output")
            data = resp.output_parsed.model_dump(mode="json")
        else:
            resp = await self._client.responses.create(
                model=model, input=inp, max_output_tokens=max_tokens
            )
            data = json.loads(resp.output_text or "{}")
        u = resp.usage
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        usage = Usage(
            provider=self.name,
            model=model,
            input_tokens=u.input_tokens,
            cached_input_tokens=cached,
            output_tokens=u.output_tokens,
            cost_usd=cost_usd(model, u.input_tokens, cached, u.output_tokens),
        )
        return data, usage
