"""LLM provider abstraction. See [docs/decisions/0007-llm-provider-abstraction.md].

The agent loop talks to one of these instead of an Ollama client directly,
so we can swap the backend at runtime via `PA_LLM_PROVIDER`. Each provider
adapter translates its native streaming protocol into the normalized
[LLMChunk] sequence the loop consumes.
"""

from backend.app.llm.base import (
    LLMChunk,
    LLMMessage,
    LLMProvider,
    LLMToolCall,
    LLMUsage,
)
from backend.app.config import get_settings

__all__ = [
    "LLMChunk",
    "LLMMessage",
    "LLMProvider",
    "LLMToolCall",
    "LLMUsage",
    "get_provider",
]


def get_provider() -> LLMProvider:
    """Construct the configured provider. Imports are lazy so an unused
    backend's SDK doesn't have to be installed."""
    s = get_settings()
    match s.llm_provider:
        case "ollama":
            from backend.app.llm.ollama import OllamaProvider

            return OllamaProvider(s)
        case "anthropic":
            from backend.app.llm.anthropic import AnthropicProvider

            return AnthropicProvider(s)
        case "openai":
            from backend.app.llm.openai import OpenAIProvider

            return OpenAIProvider(s)
        case other:
            raise ValueError(f"unknown PA_LLM_PROVIDER={other!r}")
