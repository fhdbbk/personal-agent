"""Phase 2 agent loop. See docs/decisions/0003-agent-loop.md.

Sequential ReAct: each iteration is one non-streaming Ollama call. If the
response carries tool_calls we dispatch them one at a time, append the
result as a {"role":"tool",...} message, and loop. If it carries no
tool_calls, that text is the final answer.

The loop is transport-agnostic — it talks to the WS handler through two
callbacks (`on_event`, `request_approval`) so this file knows nothing
about WebSockets, and we can unit-test it with plain async fakes.
"""

import logging
import uuid
from typing import Any, Awaitable, Callable

from ollama import AsyncClient

from backend.app.config import get_settings
from backend.app.tools.registry import TOOLS, ollama_tool_specs

log = logging.getLogger("pa.agent")

OnEvent = Callable[[dict[str, Any]], Awaitable[None]]
RequestApproval = Callable[[str, str, dict[str, Any]], Awaitable[bool]]


class AgentError(Exception):
    """The loop couldn't produce a final answer (max steps, repeated tool
    failures, etc.). The WS handler turns this into an error frame."""


def _preview(text: str, n: int = 500) -> str:
    return text if len(text) <= n else text[:n] + f"… [truncated {len(text)-n} chars]"


async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str,
    on_event: OnEvent,
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
    base_messages: list[dict[str, Any]],
    client: AsyncClient,
    on_event: OnEvent,
    request_approval: RequestApproval,
) -> str:
    """Drive the loop until the model returns a final answer.

    `base_messages` is the system prompt + history + the new user turn.
    On exit, the assistant's final text is returned; the caller persists
    that to the conversation buffer.
    """
    settings = get_settings()
    msgs = list(base_messages)  # local copy; we mutate as the loop progresses
    tool_specs = ollama_tool_specs()

    consecutive_errors = 0
    last_failed_tool: str | None = None

    for step in range(settings.agent_max_steps):
        log.info("agent cid=%s step=%d", conversation_id, step)
        resp = await client.chat(
            model=settings.ollama_model,
            messages=msgs,
            tools=tool_specs,
            stream=False,
            think=settings.ollama_think,
        )
        assistant = resp.message  # ollama-python Message (pydantic)
        tool_calls = assistant.tool_calls or []
        content = assistant.content or ""

        if not tool_calls:
            log.info(
                "agent cid=%s done step=%d reply_len=%d",
                conversation_id,
                step,
                len(content),
            )
            return content

        # Append the assistant turn (with tool_calls) as a dict so subsequent
        # iterations send a consistent transcript back to Ollama.
        msgs.append(assistant.model_dump(exclude_none=True))

        for tc in tool_calls:
            name = tc.function.name
            args = dict(tc.function.arguments or {})
            call_id = f"call_{uuid.uuid4().hex[:12]}"

            await on_event(
                {
                    "type": "tool_call",
                    "call_id": call_id,
                    "name": name,
                    "args": args,
                }
            )

            ok, result = await _dispatch_tool(
                name,
                args,
                call_id=call_id,
                on_event=on_event,
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
                if last_failed_tool == name:
                    consecutive_errors += 1
                else:
                    consecutive_errors = 1
                    last_failed_tool = name
                if consecutive_errors > settings.agent_max_retries_per_tool:
                    raise AgentError(
                        f"tool {name!r} failed {consecutive_errors} times in a row;"
                        f" last error: {result}"
                    )

            # The model gets the full result, not the truncated preview.
            msgs.append({"role": "tool", "content": result})

    raise AgentError(
        f"agent exceeded MAX_STEPS={settings.agent_max_steps} without a final answer"
    )
