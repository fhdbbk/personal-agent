"""Unit tests for the agent loop. See [backend/app/agent/loop.py] and
[docs/decisions/0003-agent-loop.md].

The loop is intentionally transport-agnostic — it talks to the WS
handler through two callbacks (`on_event`, `request_approval`) and to
Ollama through an `AsyncClient`. We fake all three here so the tests
run without a live Ollama and cover paths that smoke scripts can't
deterministically reach (max-step abort, repeated-failure abort,
multi-call stats aggregation).
"""

import pytest
from ollama import ChatResponse, Message

from backend.app.agent.loop import AgentError, run_turn
from backend.app.config import get_settings
from backend.app.tools.registry import Tool


# ---- fixtures + helpers --------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Settings is lru_cached. Tests that monkeypatch PA_* env vars
    clear the cache themselves, but reset on teardown so a test's
    settings can't bleed into the next one."""
    yield
    get_settings.cache_clear()


def _chunk(
    content: str = "",
    tool_calls: list | None = None,
    eval_count: int | None = None,
    eval_duration: int | None = None,
    prompt_eval_count: int | None = None,
) -> ChatResponse:
    """Build a ChatResponse like the ones Ollama streams. Stats fields
    are populated only on the final chunk (`done=True`), so we treat
    `eval_count is not None` as the done marker."""
    msg = Message(role="assistant", content=content, tool_calls=tool_calls)
    return ChatResponse(
        message=msg,
        eval_count=eval_count,
        eval_duration=eval_duration,
        prompt_eval_count=prompt_eval_count,
        done=eval_count is not None,
    )


def _tc(name: str, args: dict) -> dict:
    """Tool-call payload for Message(tool_calls=...). Pydantic builds
    the ToolCall from the dict shape."""
    return {"function": {"name": name, "arguments": args}}


class FakeClient:
    """Stand-in for ollama.AsyncClient. Each entry of `iterations` is
    the list of chunks the i-th call to `chat(...)` should yield."""

    def __init__(self, iterations: list[list[ChatResponse]]) -> None:
        self._iters = list(iterations)
        self.calls: list[dict] = []  # captured kwargs of each chat() call

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self._iters:
            raise AssertionError(
                "FakeClient exhausted: loop made more model calls than the test scripted"
            )
        chunks = self._iters.pop(0)

        async def gen():
            for c in chunks:
                yield c

        return gen()


class EventLog:
    """Collects on_event frames so a test can assert on the sequence."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def __call__(self, frame: dict) -> None:
        self.frames.append(frame)


async def _allow(call_id, name, args) -> bool:
    return True


async def _deny(call_id, name, args) -> bool:
    return False


# ---- happy-path -----------------------------------------------------


async def test_no_tool_calls_returns_content_and_stats():
    client = FakeClient(
        [
            [
                _chunk(content="hello "),
                _chunk(
                    content="world",
                    eval_count=2,
                    eval_duration=2_000_000_000,  # 2s
                    prompt_eval_count=100,
                ),
            ]
        ]
    )
    events = EventLog()

    final, stats = await run_turn(
        conversation_id="c1",
        base_messages=[{"role": "user", "content": "hi"}],
        client=client,
        on_event=events,
        request_approval=_allow,
    )

    assert final == "hello world"
    assert stats["eval_tokens"] == 2
    assert stats["prompt_tokens"] == 100
    assert stats["eval_seconds"] == 2.0
    assert stats["tokens_per_sec"] == 1.0
    assert stats["model_calls"] == 1
    assert stats["ttft_seconds"] is not None
    deltas = [f["delta"] for f in events.frames if f["type"] == "token"]
    assert deltas == ["hello ", "world"]


async def test_stats_sum_across_iterations(monkeypatch):
    """Multi-iteration turn: stats should aggregate across both model
    calls (this is the 'compute cost proxy' semantics from the
    token-stats session log)."""

    async def echo(text: str) -> str:
        return f"echo:{text}"

    monkeypatch.setattr(
        "backend.app.agent.loop.TOOLS",
        {"echo": Tool(name="echo", fn=echo, schema={}, requires_approval=False)},
    )
    client = FakeClient(
        [
            # call 1: just a tool_call
            [
                _chunk(
                    tool_calls=[_tc("echo", {"text": "hi"})],
                    eval_count=1,
                    eval_duration=500_000_000,
                    prompt_eval_count=10,
                )
            ],
            # call 2: final content
            [
                _chunk(
                    content="done",
                    eval_count=3,
                    eval_duration=1_500_000_000,
                    prompt_eval_count=20,
                )
            ],
        ]
    )

    final, stats = await run_turn(
        conversation_id="c1",
        base_messages=[],
        client=client,
        on_event=EventLog(),
        request_approval=_allow,
    )

    assert final == "done"
    assert stats["eval_tokens"] == 4  # 1 + 3
    assert stats["prompt_tokens"] == 30  # 10 + 20
    assert stats["eval_seconds"] == 2.0  # 0.5 + 1.5
    assert stats["model_calls"] == 2
    assert stats["tokens_per_sec"] == 2.0


# ---- tool dispatch + approval --------------------------------------


async def test_tool_call_dispatch_emits_call_and_result_frames(monkeypatch):
    async def add(a: int, b: int) -> str:
        return str(a + b)

    monkeypatch.setattr(
        "backend.app.agent.loop.TOOLS",
        {"add": Tool(name="add", fn=add, schema={}, requires_approval=False)},
    )
    client = FakeClient(
        [
            [_chunk(tool_calls=[_tc("add", {"a": 2, "b": 3})])],
            [
                _chunk(
                    content="answer is 5",
                    eval_count=4,
                    eval_duration=1_000_000_000,
                )
            ],
        ]
    )
    events = EventLog()

    final, _ = await run_turn(
        conversation_id="c1",
        base_messages=[],
        client=client,
        on_event=events,
        request_approval=_allow,
    )

    assert final == "answer is 5"
    types = [f["type"] for f in events.frames]
    assert types == ["tool_call", "tool_result", "token"]
    tr = next(f for f in events.frames if f["type"] == "tool_result")
    assert tr["ok"] is True
    assert tr["preview"] == "5"
    # The loop should also feed the tool result back into msgs for call 2.
    second_msgs = client.calls[1]["messages"]
    assert second_msgs[-1] == {"role": "tool", "content": "5"}


async def test_tool_bad_args_returns_argument_error(monkeypatch):
    """When the model passes wrong/missing kwargs the call raises
    TypeError; the loop catches it and feeds back 'argument error' so
    the model can self-correct (not the unknown-tool path)."""

    async def add(a: int, b: int) -> str:
        return str(a + b)

    monkeypatch.setattr(
        "backend.app.agent.loop.TOOLS",
        {"add": Tool(name="add", fn=add, schema={}, requires_approval=False)},
    )
    client = FakeClient(
        [
            [_chunk(tool_calls=[_tc("add", {"a": 1})])],  # missing b
            [
                _chunk(
                    content="let me retry",
                    eval_count=1,
                    eval_duration=1_000_000_000,
                )
            ],
        ]
    )
    events = EventLog()

    final, _ = await run_turn(
        conversation_id="c1",
        base_messages=[],
        client=client,
        on_event=events,
        request_approval=_allow,
    )

    assert final == "let me retry"
    tr = next(f for f in events.frames if f["type"] == "tool_result")
    assert tr["ok"] is False
    assert "argument error" in tr["preview"].lower()


async def test_client_chat_receives_options_and_tools_per_iteration():
    """Plumbing check: run_turn must hand options (incl. num_ctx) and
    tool specs to AsyncClient.chat on every iteration, and request
    streaming. Catches regressions where one of these gets dropped on
    the multi-iteration code path."""
    client = FakeClient(
        [
            [
                _chunk(
                    content="hi",
                    eval_count=1,
                    eval_duration=1_000_000_000,
                )
            ],
        ]
    )

    await run_turn(
        conversation_id="c1",
        base_messages=[{"role": "user", "content": "hello"}],
        client=client,
        on_event=EventLog(),
        request_approval=_allow,
    )

    assert len(client.calls) == 1
    kwargs = client.calls[0]
    assert kwargs["stream"] is True
    assert kwargs["options"]["num_ctx"] == get_settings().ollama_num_ctx
    assert "tools" in kwargs and isinstance(kwargs["tools"], list)
    # System+history were forwarded as messages, not lost.
    assert kwargs["messages"] == [{"role": "user", "content": "hello"}]


async def test_unknown_tool_emits_error_result_for_model(monkeypatch):
    """When the model hallucinates a tool name, the error gets fed
    back as a tool_result with ok=False so the model can self-correct."""
    monkeypatch.setattr("backend.app.agent.loop.TOOLS", {})
    client = FakeClient(
        [
            [_chunk(tool_calls=[_tc("ghost", {})])],
            [_chunk(content="ok I gave up", eval_count=1, eval_duration=1_000_000_000)],
        ]
    )
    events = EventLog()

    final, _ = await run_turn(
        conversation_id="c1",
        base_messages=[],
        client=client,
        on_event=events,
        request_approval=_allow,
    )

    assert final == "ok I gave up"
    tr = next(f for f in events.frames if f["type"] == "tool_result")
    assert tr["ok"] is False
    assert "unknown tool" in tr["preview"]


async def test_approval_approved_runs_tool(monkeypatch):
    ran_with: list[int] = []

    async def dangerous(x: int) -> str:
        ran_with.append(x)
        return "did it"

    monkeypatch.setattr(
        "backend.app.agent.loop.TOOLS",
        {
            "dangerous": Tool(
                name="dangerous", fn=dangerous, schema={}, requires_approval=True
            )
        },
    )
    client = FakeClient(
        [
            [_chunk(tool_calls=[_tc("dangerous", {"x": 1})])],
            [_chunk(content="done", eval_count=1, eval_duration=1_000_000_000)],
        ]
    )

    final, _ = await run_turn(
        conversation_id="c1",
        base_messages=[],
        client=client,
        on_event=EventLog(),
        request_approval=_allow,
    )

    assert final == "done"
    assert ran_with == [1]


async def test_approval_denied_returns_user_denied_without_running(monkeypatch):
    """Denial returns ok=True with a 'user denied' body so the model
    can adapt; the tool body must NOT run, and denial must not count
    against the retry budget."""
    ran_with: list[int] = []

    async def dangerous(x: int) -> str:
        ran_with.append(x)
        return "should not run"

    monkeypatch.setattr(
        "backend.app.agent.loop.TOOLS",
        {
            "dangerous": Tool(
                name="dangerous", fn=dangerous, schema={}, requires_approval=True
            )
        },
    )
    client = FakeClient(
        [
            [_chunk(tool_calls=[_tc("dangerous", {"x": 1})])],
            [
                _chunk(
                    content="ok, won't bother",
                    eval_count=1,
                    eval_duration=1_000_000_000,
                )
            ],
        ]
    )
    events = EventLog()

    final, _ = await run_turn(
        conversation_id="c1",
        base_messages=[],
        client=client,
        on_event=events,
        request_approval=_deny,
    )

    assert final == "ok, won't bother"
    assert ran_with == []
    tr = next(f for f in events.frames if f["type"] == "tool_result")
    assert tr["ok"] is True  # denial is not an error
    assert "denied" in tr["preview"].lower()


# ---- failure modes --------------------------------------------------


async def test_repeated_tool_failure_raises_agent_error(monkeypatch):
    """Default max_retries_per_tool=2 → the *third* consecutive
    failure of the same tool aborts the turn."""

    async def boom() -> str:
        raise RuntimeError("nope")

    monkeypatch.setattr(
        "backend.app.agent.loop.TOOLS",
        {"boom": Tool(name="boom", fn=boom, schema={}, requires_approval=False)},
    )
    client = FakeClient(
        [[_chunk(tool_calls=[_tc("boom", {})])] for _ in range(3)]
    )

    with pytest.raises(AgentError, match="3 times in a row"):
        await run_turn(
            conversation_id="c1",
            base_messages=[],
            client=client,
            on_event=EventLog(),
            request_approval=_allow,
        )


async def test_consecutive_error_counter_resets_on_different_tool(monkeypatch):
    """Pattern a,a,b,b,a,a,a:
    - First two a-fails get to counter=2 (under threshold).
    - b-fail resets to counter=1.
    - Second b-fail to counter=2.
    - Switching back to a resets to counter=1, then 2, then 3 → raise.
    Confirms the counter is keyed to the *same* tool, not total errors.
    """

    async def a() -> str:
        raise RuntimeError("a-fail")

    async def b() -> str:
        raise RuntimeError("b-fail")

    monkeypatch.setattr(
        "backend.app.agent.loop.TOOLS",
        {
            "a": Tool(name="a", fn=a, schema={}, requires_approval=False),
            "b": Tool(name="b", fn=b, schema={}, requires_approval=False),
        },
    )
    client = FakeClient(
        [
            [_chunk(tool_calls=[_tc("a", {})])],
            [_chunk(tool_calls=[_tc("a", {})])],
            [_chunk(tool_calls=[_tc("b", {})])],
            [_chunk(tool_calls=[_tc("b", {})])],
            [_chunk(tool_calls=[_tc("a", {})])],
            [_chunk(tool_calls=[_tc("a", {})])],
            [_chunk(tool_calls=[_tc("a", {})])],
        ]
    )

    with pytest.raises(AgentError, match="'a'"):
        await run_turn(
            conversation_id="c1",
            base_messages=[],
            client=client,
            on_event=EventLog(),
            request_approval=_allow,
        )


async def test_max_steps_exceeded_raises(monkeypatch):
    """If the model keeps producing tool_calls past PA_AGENT_MAX_STEPS,
    the loop aborts rather than running forever."""
    monkeypatch.setenv("PA_AGENT_MAX_STEPS", "2")
    get_settings.cache_clear()

    async def keep_going() -> str:
        return "ok"

    monkeypatch.setattr(
        "backend.app.agent.loop.TOOLS",
        {
            "loop": Tool(
                name="loop", fn=keep_going, schema={}, requires_approval=False
            )
        },
    )
    # 2 steps allowed; both produce tool_calls so the loop exits via raise.
    client = FakeClient(
        [
            [_chunk(tool_calls=[_tc("loop", {})])],
            [_chunk(tool_calls=[_tc("loop", {})])],
        ]
    )

    with pytest.raises(AgentError, match="MAX_STEPS"):
        await run_turn(
            conversation_id="c1",
            base_messages=[],
            client=client,
            on_event=EventLog(),
            request_approval=_allow,
        )
