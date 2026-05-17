"""Unit tests for the Ollama provider adapter.

Same approach as the other provider tests: stand in fake chunks via
`SimpleNamespace` and monkeypatch `_client.chat` so no live Ollama is
required. test_agent_loop.py already covers the loop end-to-end via
FakeProvider; this file is specifically about the Ollama → LLMChunk
translation.
"""

from types import SimpleNamespace

import pytest

from backend.app.config import Settings
from backend.app.llm.base import LLMMessage, LLMToolCall
from backend.app.llm.ollama import OllamaProvider
from backend.app.tools.registry import Tool


@pytest.fixture(autouse=True)
def _settings_clear():
    from backend.app.config import get_settings

    yield
    get_settings.cache_clear()


def _provider() -> OllamaProvider:
    return OllamaProvider(Settings())


def _ns(**kw):
    return SimpleNamespace(**kw)


def _ollama_chunk(*, content="", tool_calls=None, done=False, eval_count=None,
                  eval_duration=None, prompt_eval_count=None):
    """Build an ollama ChatResponse-like object. We only need attribute
    access — the SDK's pydantic model exposes the same surface."""
    return _ns(
        message=_ns(content=content, tool_calls=tool_calls),
        done=done,
        eval_count=eval_count,
        eval_duration=eval_duration,
        prompt_eval_count=prompt_eval_count,
    )


def _fake_stream(chunks: list):
    async def _gen():
        for c in chunks:
            yield c

    return _gen()


# ---- message shape -------------------------------------------------


def test_to_ollama_messages_includes_tool_calls():
    msgs = OllamaProvider._to_ollama_messages(
        [
            LLMMessage(role="user", content="add"),
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[
                    LLMToolCall(id="tc_0", name="add", arguments={"a": 1, "b": 2}),
                ],
            ),
            LLMMessage(role="tool", content="3", tool_call_id="tc_0"),
        ]
    )
    assert msgs[1] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "add", "arguments": {"a": 1, "b": 2}}}],
    }
    # Ollama tool messages don't carry an id; just role + content.
    assert msgs[2] == {"role": "tool", "content": "3"}


# ---- stream translation --------------------------------------------


async def test_text_deltas_pass_through(monkeypatch):
    p = _provider()

    chunks = [
        _ollama_chunk(content="Hello "),
        _ollama_chunk(
            content="world",
            done=True,
            eval_count=2,
            eval_duration=2_000_000_000,
            prompt_eval_count=100,
        ),
    ]

    async def fake_chat(**kwargs):
        return _fake_stream(chunks)

    monkeypatch.setattr(p._client, "chat", fake_chat)

    out = [
        c
        async for c in p.chat_stream(
            [LLMMessage(role="user", content="hi")],
            tools=[],
        )
    ]
    deltas = [c.delta_text for c in out if c.delta_text]
    assert deltas == ["Hello ", "world"]

    final = out[-1]
    assert final.done is True
    assert final.tool_calls is None
    assert final.usage.prompt_tokens == 100
    assert final.usage.completion_tokens == 2
    assert final.usage.duration_ns == 2_000_000_000


async def test_tool_calls_emitted_only_on_final_chunk_with_synthesized_ids(monkeypatch):
    """Ollama doesn't natively use tool-call ids. The adapter synthesizes
    `tc_<n>` based on position so the loop can correlate the result with
    the originating call when it flows through Anthropic/OpenAI later."""
    p = _provider()

    chunks = [
        _ollama_chunk(
            content="",
            tool_calls=[
                _ns(function=_ns(name="read_file", arguments={"path": "a.md"})),
                _ns(function=_ns(name="read_file", arguments={"path": "b.md"})),
            ],
            done=True,
            eval_count=4,
            eval_duration=1_000_000_000,
            prompt_eval_count=10,
        ),
    ]

    async def fake_chat(**kwargs):
        return _fake_stream(chunks)

    monkeypatch.setattr(p._client, "chat", fake_chat)

    out = [
        c
        async for c in p.chat_stream(
            [LLMMessage(role="user", content="read both")],
            tools=[],
        )
    ]
    final = out[-1]
    assert final.tool_calls == [
        LLMToolCall(id="tc_0", name="read_file", arguments={"path": "a.md"}),
        LLMToolCall(id="tc_1", name="read_file", arguments={"path": "b.md"}),
    ]


async def test_tools_forwarded_in_ollama_envelope(monkeypatch):
    p = _provider()

    captured: dict = {}

    async def fake_chat(**kwargs):
        captured.update(kwargs)
        return _fake_stream(
            [
                _ollama_chunk(
                    content="ok",
                    done=True,
                    eval_count=1,
                    eval_duration=1_000_000_000,
                    prompt_eval_count=1,
                )
            ]
        )

    monkeypatch.setattr(p._client, "chat", fake_chat)

    async def fn() -> str:
        return ""

    tool = Tool(
        name="echo",
        description="say hi",
        parameters={"type": "object", "properties": {}},
        fn=fn,
        requires_approval=False,
    )

    async for _ in p.chat_stream(
        [LLMMessage(role="user", content="hi")],
        tools=[tool],
    ):
        pass

    assert captured["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "say hi",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert captured["stream"] is True
    assert "options" in captured and "num_ctx" in captured["options"]
