"""Normalized LLM types + Provider Protocol.

The loop in [backend/app/agent/loop.py] consumes the chunk stream from a
provider; the provider's job is to translate its SDK's native events into
this shape. Three invariants the adapters MUST hold:

1. Tool calls are only emitted complete. Adapters buffer streaming
   tool-call deltas internally and put the fully-assembled list on the
   final chunk. The loop never sees a partial call.
2. `tool_call_id` is end-to-end. Anthropic and OpenAI both correlate
   `tool_use` blocks with their `tool_result` replies by id; the Ollama
   adapter synthesizes ids since Ollama doesn't natively use them.
3. Provider-specific knobs (Ollama `think` / `num_ctx`, Anthropic
   `max_tokens`, etc.) live inside the provider, read from Settings.
   The protocol's `chat_stream` stays narrow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Literal, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.tools.registry import Tool


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMUsage:
    prompt_tokens: int
    completion_tokens: int
    # Ollama reports decode time directly (eval_duration). Cloud adapters
    # measure wall-clock around the stream and approximate; the loop only
    # uses this for the per-turn tokens/sec figure so an approximation is
    # acceptable. None means "we don't know" — the loop falls back to 0.
    duration_ns: int | None = None


@dataclass
class LLMChunk:
    delta_text: str | None
    tool_calls: list[LLMToolCall] | None
    done: bool
    usage: LLMUsage | None


@dataclass
class LLMMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    # Populated only on assistant messages that issued tool calls; matches
    # the ids in subsequent role="tool" messages.
    tool_calls: list[LLMToolCall] | None = None
    # Populated only on role="tool" messages so the provider can correlate
    # the result with the originating tool_call.
    tool_call_id: str | None = None


class LLMProvider(Protocol):
    """A provider yields LLMChunks. `done=True` arrives exactly once,
    on the last chunk, and carries usage + any tool_calls."""

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        tools: list["Tool"],
    ) -> AsyncIterator[LLMChunk]: ...
