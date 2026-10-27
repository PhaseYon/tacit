"""Record/replay provider for tests: no network, no spend, deterministic.

Fixtures are keyed by a hash of (tier, messages, schema name). In "replay" mode a missing
fixture fails loudly with its key; "record" mode calls a real provider and saves replies.
Only record against synthetic data (never real private content).
"""

import hashlib
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .base import ChatMessage, LLMProvider, Tier, Usage


class MissingFixture(LookupError):
    pass


def fixture_key(messages: list[ChatMessage], schema: type[BaseModel] | None, tier: Tier) -> str:
    payload = json.dumps(
        {
            "tier": tier,
            "schema": schema.__name__ if schema else None,
            "messages": [[m.role, m.content] for m in messages],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class ReplayProvider:
    name = "replay"

    def __init__(
        self,
        fixtures_dir: Path,
        *,
        record_with: LLMProvider | None = None,
        responder: Callable[[list[ChatMessage], type[BaseModel] | None], dict] | None = None,
    ) -> None:
        self.dir = fixtures_dir
        self.record_with = record_with
        # Optional programmatic responder (unit tests) used when no fixture exists.
        self.responder = responder
        self.calls: list[str] = []

    def model_for(self, tier: Tier) -> str:
        return f"replay-{tier}"

    async def complete_structured(
        self,
        messages: list[ChatMessage],
        schema: type[BaseModel] | None,
        *,
        tier: Tier = "default",
        max_tokens: int = 8192,
    ) -> tuple[dict[str, Any], Usage]:
        key = fixture_key(messages, schema, tier)
        self.calls.append(key)
        path = self.dir / f"{key}.json"
        if path.exists():
            saved = json.loads(path.read_text())
            u = saved["usage"]
            return saved["response"], Usage(
                self.name,
                u["model"],
                u["input_tokens"],
                u["cached_input_tokens"],
                u["output_tokens"],
                Decimal(u["cost_usd"]),
            )
        if self.record_with is not None:
            data, usage = await self.record_with.complete_structured(
                messages, schema, tier=tier, max_tokens=max_tokens
            )
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "response": data,
                        "usage": {
                            "model": usage.model,
                            "input_tokens": usage.input_tokens,
                            "cached_input_tokens": usage.cached_input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cost_usd": str(usage.cost_usd),
                        },
                    },
                    indent=2,
                )
            )
            return data, usage
        if self.responder is not None:
            data = self.responder(messages, schema)
            return data, Usage(self.name, self.model_for(tier), 1000, 0, 100, Decimal("0.0001"))
        raise MissingFixture(f"No LLM fixture {key} (schema={schema and schema.__name__})")
