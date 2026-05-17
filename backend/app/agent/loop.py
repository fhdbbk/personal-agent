"""Phase 2 agent loop. See docs/decisions/0003-agent-loop.md.

Sequential ReAct: each iteration is one streaming call to the configured
LLM provider (see [backend/app/llm/base.py]). If the response carries
tool_calls we dispatch them one at a time, append the result as a
{"role":"tool",...} message, and loop. If it carries no tool_calls, that
text is the final answer.

The loop is transport-agnostic — it talks to the WS handler through two
callbacks (`on_event`, `request_approval`) so this file knows nothing
about WebSockets, and we can unit-test it with plain async fakes.
"""

import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from backend.app.config import get_settings
from backend.app.llm.base import LLMMessage, LLMProvider, LLMToolCall
from backend.app.tools.registry import TOOLS

log = logging.getLogger("pa.agent")

OnEvent = Callable[[dict[str, Any]], Awaitable[None]]
RequestApproval = Callable[[str, str, dict[str, Any]], Awaitable[bool]]


class AgentError(Exception):
    """The loop couldn't produce a final answer (max steps, repeated tool
    failures, etc.). The WS handler turns this into an error frame."""


def _preview(text: str, n: int = 500) -> str:
    return text if len(text) <= n else text[:n] + f"… [truncated {len(text)-n} chars]"


def _make_stats(
    eval_tokens: int,
    eval_ns: int,
    prompt_tokens: int,
    model_calls: int,
    ttft_seconds: float | None,
) -> dict[str, Any]:
    eval_seconds = eval_ns / 1e9 if eval_ns else 0.0
    tps = eval_tokens / eval_seconds if eval_seconds > 0 else 0.0
    return {
        "eval_tokens": eval_tokens,
        "prompt_tokens": prompt_tokens,
        "eval_seconds": round(eval_seconds, 2),
        "tokens_per_sec": round(tps, 1),
        "model_calls": model_calls,
        # Wall-clock seconds from the start of the turn until the first
        # text token is observed in the stream. None if the turn produced
        # no text (e.g. tool-only iterations followed by an error). On a
        # cold model load this dominates total latency, which is exactly
        # why we surface it separately.
        "ttft_seconds": round(ttft_seconds, 2) if ttft_seconds is not None else None,
    }


async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str,
    request_approval: RequestApproval,
) -> tuple[bool, str]:
    """Execute one tool call. Returns (ok, result_text).

    `ok=False` means the result is an error string the model should see and
    self-correct from. The loop's retry counter only ticks on `ok=False`.
    Approval denial returns ok=True with a "user denied" body — the model
    can adapt, but we don't want to count denial as an error.
    """
    tool = TOOLS.get(name)
    if tool is None:
        return False, f"unknown tool: {name!r}. available: {list(TOOLS)}"

    if tool.requires_approval and not get_settings().agent_auto_approve:
        approved = await request_approval(call_id, name, args)
        if not approved:
            return True, "User denied this action."

    try:
        result = await tool.fn(**args)
    except TypeError as e:
        # Wrong/missing kwargs — the model passed bad args. Feed the message back.
        return False, f"argument error: {e}"
    except Exception as e:
        log.warning("tool %s failed: %s", name, e)
        return False, f"{type(e).__name__}: {e}"

    if not isinstance(result, str):
        result = str(result)
    return True, result


async def run_turn(
    *,
    conversation_id: str,
    base_messages: list[LLMMessage],
    provider: LLMProvider,
    on_event: OnEvent,
    request_approval: RequestApproval,
) -> tuple[str, dict[str, Any]]:
    """Drive the loop until the model returns a final answer.

    `base_messages` is the system prompt + history + the new user turn.
    Returns `(final_text, stats)`. Stats sums the provider's per-iteration
    usage so the caller can report tokens/sec for the whole turn (a
    multi-step turn hits the model N times).
    """
    settings = get_settings()
    msgs: list[LLMMessage] = list(base_messages)  # local copy; mutated below
    tools = list(TOOLS.values())

    consecutive_errors = 0
    last_failed_tool: str | None = None

    total_eval_tokens = 0
    total_eval_ns = 0
    total_prompt_tokens = 0
    model_calls = 0
    t_start = time.perf_counter()
    t_first_token: float | None = None

    for step in range(settings.agent_max_steps):
        log.info("agent cid=%s step=%d", conversation_id, step)
        # Stream every iteration. Text deltas are forwarded to the UI as
        # token frames as they arrive; tool_calls land on the final chunk
        # (the provider's job to assemble them).
        content_chunks: list[str] = []
        final_tool_calls: list[LLMToolCall] = []
        usage = None

        async for chunk in provider.chat_stream(msgs, tools):
            if chunk.delta_text:
                if t_first_token is None:
                    t_first_token = time.perf_counter()
                content_chunks.append(chunk.delta_text)
                await on_event({"type": "token", "delta": chunk.delta_text})
            if chunk.done:
                if chunk.tool_calls:
                    final_tool_calls = chunk.tool_calls
                usage = chunk.usage

        if usage is not None:
            total_eval_tokens += usage.completion_tokens
            total_prompt_tokens += usage.prompt_tokens
            total_eval_ns += usage.duration_ns or 0
            model_calls += 1

        content = "".join(content_chunks)
        tool_calls = final_tool_calls

        if not tool_calls:
            ttft = (t_first_token - t_start) if t_first_token is not None else None
            log.info(
                "agent cid=%s done step=%d reply_len=%d prompt_tokens=%d eval_tokens=%d eval_s=%.2f ttft_s=%s",
                conversation_id,
                step,
                len(content),
                total_prompt_tokens,
                total_eval_tokens,
                total_eval_ns / 1e9,
                f"{ttft:.2f}" if ttft is not None else "-",
            )
            return content, _make_stats(
                total_eval_tokens,
                total_eval_ns,
                total_prompt_tokens,
                model_calls,
                ttft,
            )

        # Append the assistant turn (with tool_calls) so the next iteration
        # sees a consistent transcript.
        msgs.append(
            LLMMessage(role="assistant", content=content, tool_calls=tool_calls)
        )

        for tc in tool_calls:
            call_id = f"call_{uuid.uuid4().hex[:12]}"

            await on_event(
                {
                    "type": "tool_call",
                    "call_id": call_id,
                    "name": tc.name,
                    "args": tc.arguments,
                }
            )

            ok, result = await _dispatch_tool(
                tc.name,
                tc.arguments,
                call_id=call_id,
                request_approval=request_approval,
            )

            await on_event(
                {
                    "type": "tool_result",
                    "call_id": call_id,
                    "ok": ok,
                    "preview": _preview(result),
                }
            )

            if ok:
                consecutive_errors = 0
                last_failed_tool = None
            else:
                if last_failed_tool == tc.name:
                    consecutive_errors += 1
                else:
                    consecutive_errors = 1
                    last_failed_tool = tc.name
                if consecutive_errors > settings.agent_max_retries_per_tool:
                    raise AgentError(
                        f"tool {tc.name!r} failed {consecutive_errors} times in a row;"
                        f" last error: {result}"
                    )

            # The model gets the full result, not the truncated preview.
            # tool_call_id is what Anthropic / OpenAI use to correlate this
            # result with the originating tool_use block; Ollama ignores it.
            msgs.append(
                LLMMessage(role="tool", content=result, tool_call_id=tc.id)
            )

    raise AgentError(
        f"agent exceeded MAX_STEPS={settings.agent_max_steps} without a final answer"
    )
