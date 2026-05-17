"""Anthropic provider — translates Anthropic Messages streaming events
into the normalized LLMChunk stream.

The translation work is mostly in two places:

* **Message shape**. Anthropic separates `system` from the user/assistant
  list, uses content blocks for assistant turns (`text` + `tool_use`),
  and folds tool results into a single user message with one or more
  `tool_result` blocks. The loop's flat `LLMMessage` list is converted
  in `_to_anthropic_messages`.

* **Event stream**. Anthropic emits events per content block:
  `content_block_start`/`_delta`/`_stop` framing each text or tool_use
  block. Tool-call arguments arrive as JSON fragments via
  `input_json_delta` and are concatenated then parsed at block stop.
  We emit text deltas immediately and surface the assembled tool calls
  + usage on the final chunk.
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator

from anthropic import AsyncAnthropic

from backend.app.config import Settings
from backend.app.llm.base import LLMChunk, LLMMessage, LLMToolCall, LLMUsage
from backend.app.tools.registry import Tool, anthropic_tool_specs


class AnthropicProvider:
    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "PA_LLM_PROVIDER=anthropic but PA_ANTHROPIC_API_KEY is empty"
            )
        self._settings = settings
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    @staticmethod
    def _to_anthropic_messages(
        messages: list[LLMMessage],
    ) -> tuple[str | None, list[dict]]:
        """Pull the system prompt out, then build user/assistant turns.

        Consecutive `tool` messages are folded into one user message with
        a `tool_result` block per call — Anthropic requires user/assistant
        alternation and bundles tool replies as a single user turn.
        """
        system: str | None = None
        rest: list[LLMMessage] = []
        for m in messages:
            if m.role == "system":
                # If multiple system messages slip in, concatenate. Should
                # not happen in practice — the loop only emits one.
                system = (system + "\n\n" + m.content) if system else m.content
            else:
                rest.append(m)

        out: list[dict] = []
        i = 0
        while i < len(rest):
            m = rest[i]
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
                i += 1
            elif m.role == "assistant":
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc.id,
                                "name": tc.name,
                                "input": tc.arguments,
                            }
                        )
                out.append({"role": "assistant", "content": blocks})
                i += 1
            elif m.role == "tool":
                results: list[dict] = []
                while i < len(rest) and rest[i].role == "tool":
                    t = rest[i]
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": t.tool_call_id or "",
                            "content": t.content,
                        }
                    )
                    i += 1
                out.append({"role": "user", "content": results})
            else:  # pragma: no cover — Literal restricts roles
                i += 1
        return system, out

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        tools: list[Tool],
    ) -> AsyncIterator[LLMChunk]:
        system, anthropic_msgs = self._to_anthropic_messages(messages)

        kwargs: dict = {
            "model": self._settings.anthropic_model,
            "max_tokens": self._settings.anthropic_max_tokens,
            "messages": anthropic_msgs,
            "stream": True,
        }
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = anthropic_tool_specs(tools)

        # Tool-use content blocks arrive incrementally — we accumulate the
        # partial JSON keyed by content-block index and finalize on stop.
        tool_buffers: dict[int, dict] = {}
        completed: list[LLMToolCall] = []
        prompt_tokens = 0
        completion_tokens = 0
        t0 = time.perf_counter_ns()

        stream = await self._client.messages.create(**kwargs)
        async for event in stream:
            etype = getattr(event, "type", None)
            if etype == "message_start":
                usage = getattr(event.message, "usage", None)
                if usage is not None:
                    prompt_tokens = getattr(usage, "input_tokens", 0) or 0
            elif etype == "content_block_start":
                block = event.content_block
                if getattr(block, "type", None) == "tool_use":
                    tool_buffers[event.index] = {
                        "id": block.id,
                        "name": block.name,
                        "partial": "",
                    }
            elif etype == "content_block_delta":
                delta = event.delta
                dtype = getattr(delta, "type", None)
                if dtype == "text_delta":
                    yield LLMChunk(
                        delta_text=delta.text,
                        tool_calls=None,
                        done=False,
                        usage=None,
                    )
                elif dtype == "input_json_delta":
                    buf = tool_buffers.get(event.index)
                    if buf is not None:
                        buf["partial"] += delta.partial_json
            elif etype == "content_block_stop":
                buf = tool_buffers.pop(event.index, None)
                if buf is not None:
                    try:
                        args = json.loads(buf["partial"]) if buf["partial"] else {}
                    except json.JSONDecodeError:
                        # Malformed JSON shouldn't reach us, but if it does,
                        # surface an empty-args call so the loop can dispatch
                        # and the tool's TypeError feeds back to the model.
                        args = {}
                    completed.append(
                        LLMToolCall(id=buf["id"], name=buf["name"], arguments=args)
                    )
            elif etype == "message_delta":
                usage = getattr(event, "usage", None)
                if usage is not None:
                    completion_tokens = getattr(usage, "output_tokens", 0) or 0

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
