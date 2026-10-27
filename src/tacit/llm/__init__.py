"""LLM providers behind one interface, used only for Graphiti extraction.
Selected by [llm] provider in config.toml (anthropic | openai | replay)."""

from functools import lru_cache
from pathlib import Path

from .. import secrets
from ..config import get_config
from .base import ChatMessage, LLMNotConfigured, LLMProvider, Tier, Usage

__all__ = ["ChatMessage", "LLMNotConfigured", "LLMProvider", "Tier", "Usage", "get_provider"]


@lru_cache
def get_provider() -> LLMProvider:
    c = get_config().llm
    if c.provider == "anthropic":
        key = secrets.get("anthropic_api_key")
        if not key:
            raise LLMNotConfigured("Set ANTHROPIC_API_KEY or run `tacit auth anthropic`")
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(key, c.model, c.small_model)
    if c.provider == "openai":
        key = secrets.get("openai_api_key")
        if not key:
            raise LLMNotConfigured("Set OPENAI_API_KEY or run `tacit auth openai`")
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(key, c.model, c.small_model)
    if c.provider == "replay":
        if not c.replay_dir:
            raise LLMNotConfigured("Set [llm] replay_dir to use the replay provider")
        from .replay import ReplayProvider

        return ReplayProvider(Path(c.replay_dir))
    raise LLMNotConfigured(f"Unknown LLM provider {c.provider!r}")
