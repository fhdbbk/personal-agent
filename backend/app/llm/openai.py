"""OpenAI provider — translates ChatCompletions streaming chunks into
the normalized LLMChunk stream.

OpenAI's wire shape is the one Ollama mirrors, so the translation work
is mostly cosmetic — `tool_call_id` lives on tool messages, tool-call
arguments arrive as JSON string fragments to be concatenated then
parsed, and usage comes in a trailing chunk after the model has stopped
(only when `stream_options={"include_usage": True}` is set).
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator

from openai import AsyncOpenAI

from backend.app.config import Settings
from backend.app.llm.base import LLMChunk, LLMMessage, LLMToolCall, LLMUsage
from backend.app.tools.registry import Tool, openai_tool_specs


class OpenAIProvider:
    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise RuntimeError(
                "PA_LLM_PROVIDER=openai but PA_OPENAI_API_KEY is empty"
            )
        self._settings = settings
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    @staticmethod
    def _to_openai_messages(messages: list[LLMMessage]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": m.content,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    # OpenAI expects the arguments as a
                                    # JSON-encoded string, not a dict.
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in m.tool_calls
                        ],
                    }
                )
            elif m.role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.tool_call_id or "",
                        "content": m.content,
                    }
                )
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        tools: list[Tool],
    ) -> AsyncIterator[LLMChunk]:
        kwargs: dict = {
            "model": self._settings.openai_model,
            "messages": self._to_openai_messages(messages),
            "stream": True,
            # Without this the final chunk lacks usage. Costs us nothing.
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = openai_tool_specs(tools)

        # index -> {id, name, partial_args}. OpenAI streams tool-call
        # fragments indexed by the call's position in the assistant turn;
        # only the first fragment for an index carries id + name.
        buffers: dict[int, dict] = {}
        prompt_tokens = 0
        completion_tokens = 0
        t0 = time.perf_counter_ns()

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                prompt_tokens = chunk.usage.prompt_tokens or 0
                completion_tokens = chunk.usage.completion_tokens or 0

            if not chunk.choices:
                # The trailing usage-only chunk has choices=[]. We've
                # captured its usage above; nothing else to do.
                continue

            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                yield LLMChunk(
                    delta_text=delta.content,
                    tool_calls=None,
                    done=False,
                    usage=None,
                )

            for tcd in getattr(delta, "tool_calls", None) or []:
                idx = tcd.index
                buf = buffers.setdefault(idx, {"id": "", "name": "", "partial": ""})
                if getattr(tcd, "id", None):
                    buf["id"] = tcd.id
                fn = getattr(tcd, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        buf["name"] = fn.name
                    frag = getattr(fn, "arguments", None)
                    if frag:
                        buf["partial"] += frag

        completed: list[LLMToolCall] = []
        for idx in sorted(buffers):
            buf = buffers[idx]
            try:
                args = json.loads(buf["partial"]) if buf["partial"] else {}
            except json.JSONDecodeError:
                args = {}
            completed.append(
                LLMToolCall(id=buf["id"], name=buf["name"], arguments=args)
            )

        yield LLMChunk(
            delta_text=None,
            tool_calls=completed if completed else None,
            done=True,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ns=time.perf_counter_ns() - t0,
            ),
        )
