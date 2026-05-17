"""Unit tests for the Anthropic provider adapter.

We test the translation surface — message-shape conversion and event-stream
consumption — without touching the network. The SDK's event objects are
attribute bags, so we stand them in with `SimpleNamespace`.
"""

from types import SimpleNamespace

import pytest

from backend.app.config import Settings
from backend.app.llm.anthropic import AnthropicProvider
from backend.app.llm.base import LLMMessage, LLMToolCall
from backend.app.tools.registry import Tool


@pytest.fixture(autouse=True)
def _settings_clear():
    from backend.app.config import get_settings

    yield
    get_settings.cache_clear()


def _provider() -> AnthropicProvider:
    """A provider with a stub key — we monkeypatch `_client.messages.create`
    in each test so no real API call is made."""
    s = Settings(
        llm_provider="anthropic",
        anthropic_api_key="stub",
        anthropic_model="claude-haiku-4-5",
        anthropic_max_tokens=512,
    )
    return AnthropicProvider(s)


# ---- message shape -------------------------------------------------


def test_system_pulled_out_into_system_kwarg():
    system, msgs = AnthropicProvider._to_anthropic_messages(
        [
            LLMMessage(role="system", content="be brief"),
            LLMMessage(role="user", content="hi"),
        ]
    )
    assert system == "be brief"
    assert msgs == [{"role": "user", "content": "hi"}]


def test_assistant_with_tool_calls_becomes_content_blocks():
    _system, msgs = AnthropicProvider._to_anthropic_messages(
        [
            LLMMessage(role="user", content="add 1 + 2"),
            LLMMessage(
                role="assistant",
                content="I'll use the calculator.",
                tool_calls=[LLMToolCall(id="toolu_1", name="add", arguments={"a": 1, "b": 2})],
            ),
            LLMMessage(role="tool", content="3", tool_call_id="toolu_1"),
        ]
    )
    assert msgs[1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I'll use the calculator."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "add",
                "input": {"a": 1, "b": 2},
            },
        ],
    }
    # Tool result folded into a user turn with a tool_result block.
    assert msgs[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "3"},
        ],
    }


def test_consecutive_tool_messages_fold_into_one_user_turn():
    """Anthropic requires user/assistant alternation. Our loop emits one
    tool message per tool call sequentially; the adapter must fold them
    into a single user message with N tool_result blocks."""
    _system, msgs = AnthropicProvider._to_anthropic_messages(
        [
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[
                    LLMToolCall(id="t1", name="a", arguments={}),
                    LLMToolCall(id="t2", name="b", arguments={}),
                ],
            ),
            LLMMessage(role="tool", content="result1", tool_call_id="t1"),
            LLMMessage(role="tool", content="result2", tool_call_id="t2"),
        ]
    )
    # One assistant turn, one user turn (with both tool_results).
    assert len(msgs) == 2
    assert msgs[0]["role"] == "assistant"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "result1"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "result2"},
    ]


def test_assistant_with_empty_content_omits_text_block():
    """Anthropic rejects empty text blocks. When the assistant only
    produced tool_calls (no preamble text), we must omit the text block."""
    _system, msgs = AnthropicProvider._to_anthropic_messages(
        [
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[LLMToolCall(id="t", name="x", arguments={})],
            ),
        ]
    )
    assert msgs[0]["content"] == [
        {"type": "tool_use", "id": "t", "name": "x", "input": {}},
    ]


# ---- event-stream translation --------------------------------------


def _ns(**kw):
    return SimpleNamespace(**kw)


def _fake_stream(events: list):
    async def _gen():
        for e in events:
            yield e

    return _gen()


async def test_text_deltas_emit_as_they_arrive(monkeypatch):
    p = _provider()

    events = [
        _ns(type="message_start", message=_ns(usage=_ns(input_tokens=42))),
        _ns(type="content_block_start", index=0, content_block=_ns(type="text")),
        _ns(
            type="content_block_delta",
            index=0,
            delta=_ns(type="text_delta", text="Hello "),
        ),
        _ns(
            type="content_block_delta",
            index=0,
            delta=_ns(type="text_delta", text="world"),
        ),
        _ns(type="content_block_stop", index=0),
        _ns(type="message_delta", usage=_ns(output_tokens=7)),
        _ns(type="message_stop"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(events)

    monkeypatch.setattr(p._client.messages, "create", fake_create)

    chunks = [
        c
        async for c in p.chat_stream(
            [LLMMessage(role="user", content="say hi")],
            tools=[],
        )
    ]

    text_chunks = [c for c in chunks if c.delta_text]
    assert [c.delta_text for c in text_chunks] == ["Hello ", "world"]

    final = chunks[-1]
    assert final.done is True
    assert final.tool_calls is None
    assert final.usage is not None
    assert final.usage.prompt_tokens == 42
    assert final.usage.completion_tokens == 7


async def test_tool_call_assembles_from_input_json_fragments(monkeypatch):
    """Anthropic streams the tool's input as a sequence of JSON fragments
    that have to be concatenated then parsed. Verify the adapter does that
    correctly and lands the assembled call on the final chunk."""
    p = _provider()

    events = [
        _ns(type="message_start", message=_ns(usage=_ns(input_tokens=10))),
        _ns(
            type="content_block_start",
            index=0,
            content_block=_ns(type="tool_use", id="toolu_abc", name="read_file"),
        ),
        _ns(
            type="content_block_delta",
            index=0,
            delta=_ns(type="input_json_delta", partial_json='{"pa'),
        ),
        _ns(
            type="content_block_delta",
            index=0,
            delta=_ns(type="input_json_delta", partial_json='th": "notes.md"}'),
        ),
        _ns(type="content_block_stop", index=0),
        _ns(type="message_delta", usage=_ns(output_tokens=3)),
        _ns(type="message_stop"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(events)

    monkeypatch.setattr(p._client.messages, "create", fake_create)

    chunks = [
        c
        async for c in p.chat_stream(
            [LLMMessage(role="user", content="read notes")],
            tools=[],
        )
    ]

    # No text was streamed.
    assert all(c.delta_text is None for c in chunks)
    final = chunks[-1]
    assert final.done is True
    assert final.tool_calls == [
        LLMToolCall(id="toolu_abc", name="read_file", arguments={"path": "notes.md"})
    ]


async def test_tools_forwarded_in_anthropic_schema(monkeypatch):
    """The adapter must wrap our canonical Tool list in Anthropic's
    `{name, description, input_schema}` shape — not Ollama's nested
    `{type:"function", function:{...}}` envelope."""
    p = _provider()

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_stream(
            [
                _ns(type="message_start", message=_ns(usage=_ns(input_tokens=1))),
                _ns(type="message_delta", usage=_ns(output_tokens=1)),
                _ns(type="message_stop"),
            ]
        )

    monkeypatch.setattr(p._client.messages, "create", fake_create)

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
            "name": "echo",
            "description": "say hi",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    # max_tokens is required by Anthropic — verify the provider supplied it.
    assert captured["max_tokens"] == 512
