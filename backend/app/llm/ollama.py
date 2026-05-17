"""Ollama provider — translates ollama.AsyncClient streaming events
into the normalized LLMChunk stream.

Ollama's wire shape is the canonical one our loop was built around, so
this adapter is essentially a re-emit: text content goes out chunk by
chunk, and on the final chunk we surface the tool_calls (with synthesized
ids, since Ollama doesn't use them natively) and usage stats.
"""

from __future__ import annotations

from typing import AsyncIterator

from ollama import AsyncClient

from backend.app.config import Settings, ollama_options
from backend.app.llm.base import LLMChunk, LLMMessage, LLMToolCall, LLMUsage
from backend.app.tools.registry import Tool, ollama_tool_specs


class OllamaProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # ollama forwards kwargs to httpx.AsyncClient. For streaming calls
        # the read-timeout becomes a per-chunk idle timeout, which is what
        # we want: a long generation is fine, a stalled connection aborts.
        self._client = AsyncClient(
            host=settings.ollama_host,
            timeout=settings.request_timeout_s,
        )

    @staticmethod
    def _to_ollama_messages(messages: list[LLMMessage]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": m.content,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": tc.name,
                                    "arguments": tc.arguments,
                                }
                            }
                            for tc in m.tool_calls
                        ],
                    }
                )
            else:
                # Ollama tool messages don't carry an id; the content is enough.
                out.append({"role": m.role, "content": m.content})
        return out

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        tools: list[Tool],
    ) -> AsyncIterator[LLMChunk]:
        stream = await self._client.chat(
            model=self._settings.ollama_model,
            messages=self._to_ollama_messages(messages),
            tools=ollama_tool_specs(tools),
            stream=True,
            think=self._settings.ollama_think,
            options=ollama_options(),
        )

        final_tool_calls: list[LLMToolCall] = []
        async for chunk in stream:
            msg = chunk.message
            delta = msg.content or None

            # tool_calls on Ollama arrive complete in a single chunk (typically
            # the last). If a future Ollama version splits them across chunks,
            # the last one observed wins — same as the pre-abstraction loop.
            if msg.tool_calls:
                final_tool_calls = [
                    LLMToolCall(
                        id=f"tc_{i}",
                        name=tc.function.name,
                        arguments=dict(tc.function.arguments or {}),
                    )
                    for i, tc in enumerate(msg.tool_calls)
                ]

            is_final = bool(getattr(chunk, "done", False))
            usage: LLMUsage | None = None
            if is_final:
                usage = LLMUsage(
                    prompt_tokens=int(getattr(chunk, "prompt_eval_count", 0) or 0),
                    completion_tokens=int(getattr(chunk, "eval_count", 0) or 0),
                    duration_ns=int(getattr(chunk, "eval_duration", 0) or 0),
                )

            yield LLMChunk(
                delta_text=delta,
                tool_calls=final_tool_calls if is_final and final_tool_calls else None,
                done=is_final,
                usage=usage,
            )
