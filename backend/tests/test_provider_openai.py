"""Unit tests for the OpenAI provider adapter.

Same approach as the Anthropic tests: stand in `SimpleNamespace` for the
SDK's chunk objects and monkeypatch `_client.chat.completions.create`.
"""

import json
from types import SimpleNamespace

import pytest

from backend.app.config import Settings
from backend.app.llm.base import LLMMessage, LLMToolCall
from backend.app.llm.openai import OpenAIProvider
from backend.app.tools.registry import Tool


@pytest.fixture(autouse=True)
def _settings_clear():
    from backend.app.config import get_settings

    yield
    get_settings.cache_clear()


def _provider() -> OpenAIProvider:
    s = Settings(
        llm_provider="openai",
        openai_api_key="stub",
        openai_model="gpt-4o-mini",
    )
    return OpenAIProvider(s)


# ---- message shape -------------------------------------------------


def test_assistant_tool_calls_become_openai_tool_calls():
    msgs = OpenAIProvider._to_openai_messages(
        [
            LLMMessage(role="user", content="hi"),
            LLMMessage(
                role="assistant",
                content="thinking…",
                tool_calls=[
                    LLMToolCall(id="call_1", name="add", arguments={"a": 1, "b": 2}),
                ],
            ),
            LLMMessage(role="tool", content="3", tool_call_id="call_1"),
        ]
    )
    assert msgs[1] == {
        "role": "assistant",
        "content": "thinking…",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "add",
                    "arguments": json.dumps({"a": 1, "b": 2}),
                },
            }
        ],
    }
    # OpenAI tool messages carry tool_call_id directly (unlike Anthropic
    # which folds them into a user turn with tool_result blocks).
    assert msgs[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "3",
    }


# ---- streaming chunk translation -----------------------------------


def _ns(**kw):
    return SimpleNamespace(**kw)


def _chunk(*, content=None, tool_calls=None, usage=None):
    """Build a ChatCompletionChunk-shaped namespace. choices=[] when the
    chunk only carries usage."""
    choices = []
    if content is not None or tool_calls is not None:
        choices = [_ns(delta=_ns(content=content, tool_calls=tool_calls))]
    return _ns(choices=choices, usage=usage)


def _fake_stream(chunks: list):
    async def _gen():
        for c in chunks:
            yield c

    return _gen()


async def test_text_deltas_emit_as_they_arrive(monkeypatch):
    p = _provider()

    chunks_in = [
        _chunk(content="Hello "),
        _chunk(content="world"),
        _chunk(usage=_ns(prompt_tokens=12, completion_tokens=2)),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(chunks_in)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [
        c
        async for c in p.chat_stream(
            [LLMMessage(role="user", content="say hi")],
            tools=[],
        )
    ]

    text_chunks = [c for c in out if c.delta_text]
    assert [c.delta_text for c in text_chunks] == ["Hello ", "world"]

    final = out[-1]
    assert final.done is True
    assert final.tool_calls is None
    assert final.usage.prompt_tokens == 12
    assert final.usage.completion_tokens == 2


async def test_tool_call_assembles_from_argument_fragments(monkeypatch):
    """OpenAI streams tool-call arguments as JSON string fragments. The
    first fragment carries id+name; subsequent fragments only append to
    arguments. Verify the adapter rebuilds them correctly."""
    p = _provider()

    chunks_in = [
        _chunk(
            tool_calls=[
                _ns(
                    index=0,
                    id="call_xyz",
                    function=_ns(name="read_file", arguments='{"pa'),
                )
            ]
        ),
        _chunk(
            tool_calls=[
                _ns(
                    index=0,
                    id=None,
                    function=_ns(name=None, arguments='th": "notes.md"}'),
                )
            ]
        ),
        _chunk(usage=_ns(prompt_tokens=5, completion_tokens=8)),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(chunks_in)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [
        c
        async for c in p.chat_stream(
            [LLMMessage(role="user", content="read notes")],
            tools=[],
        )
    ]

    assert all(c.delta_text is None for c in out)
    final = out[-1]
    assert final.done is True
    assert final.tool_calls == [
        LLMToolCall(id="call_xyz", name="read_file", arguments={"path": "notes.md"})
    ]


async def test_multiple_parallel_tool_calls_assembled_by_index(monkeypatch):
    """When OpenAI emits parallel tool calls, fragments for different
    calls are interleaved but keyed by `index`. The adapter must keep
    the buffers separate and emit both at the end."""
    p = _provider()

    chunks_in = [
        _chunk(
            tool_calls=[
                _ns(index=0, id="call_a", function=_ns(name="a", arguments="{}")),
                _ns(index=1, id="call_b", function=_ns(name="b", arguments="{")),
            ]
        ),
        _chunk(
            tool_calls=[
                _ns(index=1, id=None, function=_ns(name=None, arguments='"k": 1}')),
            ]
        ),
        _chunk(usage=_ns(prompt_tokens=1, completion_tokens=1)),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(chunks_in)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [
        c
        async for c in p.chat_stream(
            [LLMMessage(role="user", content="hi")],
            tools=[],
        )
    ]

    final = out[-1]
    assert final.tool_calls == [
        LLMToolCall(id="call_a", name="a", arguments={}),
        LLMToolCall(id="call_b", name="b", arguments={"k": 1}),
    ]


async def test_tools_forwarded_in_openai_schema(monkeypatch):
    p = _provider()

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_stream([_chunk(usage=_ns(prompt_tokens=1, completion_tokens=1))])

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

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
    assert captured["stream_options"] == {"include_usage": True}
