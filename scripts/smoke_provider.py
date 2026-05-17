"""Provider-agnostic smoke test.

Constructs the configured LLM provider via [backend.app.llm.get_provider],
streams a one-shot "say pong" prompt, and prints the assembled reply +
usage. Useful for verifying credentials and connectivity for whichever
backend is currently selected — Ollama, Anthropic, or OpenAI.

Run:
  PA_LLM_PROVIDER=ollama    uv run python scripts/smoke_provider.py
  PA_LLM_PROVIDER=anthropic uv run python scripts/smoke_provider.py
  PA_LLM_PROVIDER=openai    uv run python scripts/smoke_provider.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.config import get_settings
from backend.app.llm import LLMMessage, get_provider


async def main() -> int:
    settings = get_settings()
    provider = get_provider()
    print(f"provider={settings.llm_provider!r} class={type(provider).__name__}")

    messages = [
        LLMMessage(
            role="system",
            content="You are a smoke test. Reply with exactly one word: pong.",
        ),
        LLMMessage(role="user", content="ping"),
    ]

    t0 = time.perf_counter()
    chunks: list[str] = []
    usage = None
    async for chunk in provider.chat_stream(messages, tools=[]):
        if chunk.delta_text:
            chunks.append(chunk.delta_text)
        if chunk.done:
            usage = chunk.usage

    elapsed = time.perf_counter() - t0
    reply = "".join(chunks).strip()
    print(f"reply ({elapsed:.2f}s): {reply!r}")
    if usage:
        print(
            f"usage: prompt={usage.prompt_tokens} "
            f"completion={usage.completion_tokens} "
            f"duration_ns={usage.duration_ns}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
