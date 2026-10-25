"""LLM providers behind one interface. Select with LLM_PROVIDER (anthropic|openai|replay)."""

from functools import lru_cache
from pathlib import Path

from ..config import get_settings
from .base import ChatMessage, LLMNotConfigured, LLMProvider, Tier, Usage

__all__ = ["ChatMessage", "LLMNotConfigured", "LLMProvider", "Tier", "Usage", "get_provider"]


@lru_cache
def get_provider() -> LLMProvider:
    s = get_settings()
    if s.llm_provider == "anthropic":
        if not s.anthropic_api_key.get_secret_value():
            raise LLMNotConfigured("Set ANTHROPIC_API_KEY to use the Anthropic provider")
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            s.anthropic_api_key.get_secret_value(), s.llm_model, s.llm_small_model
        )
    if s.llm_provider == "openai":
        if not s.openai_api_key.get_secret_value():
            raise LLMNotConfigured("Set OPENAI_API_KEY to use the OpenAI provider")
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(s.openai_api_key.get_secret_value(), s.llm_model, s.llm_small_model)
    if s.llm_provider == "replay":
        from .replay import ReplayProvider

        return ReplayProvider(Path(s.llm_replay_dir))
    raise LLMNotConfigured(f"Unknown LLM_PROVIDER={s.llm_provider!r}")
