# 0007 — LLM provider abstraction: Ollama + Anthropic + OpenAI

**Status:** Accepted
**Date:** 2026-05-16
**Phase:** 2

## Context

Until this ADR the entire codebase was bolted to the `ollama` SDK.
`ollama.AsyncClient`, `ChatResponse`, `Message`, and `ToolCall` types
leaked into [backend/app/agent/loop.py](../../backend/app/agent/loop.py),
[backend/app/api/chat.py](../../backend/app/api/chat.py), and the test
suite. Tool schemas under [backend/app/tools/](../../backend/app/tools/)
were written in Ollama's tool-format dialect (which mirrors OpenAI's,
but isn't Anthropic's).

Two pressures forced this to change:

1. Local Ollama on CPU is good enough for tinkering but not for harder
   tool-use scenarios — small open models still trip on JSON schemas and
   multi-step ReAct. Being able to flip to Claude Haiku or `gpt-4o-mini`
   for a turn would close the gap without abandoning the local-first
   ethos.
2. The hosting plan (deferred, but next on the list) needs a hosted LLM
   API on the server — running Ollama on a cheap VPS doesn't fit the
   $5–15/mo budget. The provider seam is a prerequisite, not an optional
   cleanup.

## Decision

Put a thin `LLMProvider` Protocol between the agent loop and the LLM
SDKs. Ship three concrete adapters: Ollama (default; preserves the
local-first dev experience), Anthropic, and OpenAI.

### Normalized types ([backend/app/llm/base.py](../../backend/app/llm/base.py))

Five dataclasses + one Protocol — ~70 lines, no framework:

- `LLMMessage` — role/content/optional tool_calls/optional tool_call_id.
- `LLMToolCall` — id/name/arguments. The id is end-to-end; the Ollama
  adapter synthesizes `tc_<n>` since Ollama doesn't natively use ids.
- `LLMChunk` — delta_text/tool_calls/done/usage. Tool calls only ever
  appear on the final chunk, fully assembled.
- `LLMUsage` — prompt_tokens/completion_tokens/duration_ns.
- `LLMProvider` Protocol — one method, `chat_stream(messages, tools) →
  AsyncIterator[LLMChunk]`.

Three invariants the adapters must hold:

1. **Tool calls are only emitted complete.** Adapters buffer streaming
   tool-call deltas internally and emit the assembled list on the final
   chunk. The loop never sees partial JSON.
2. **`tool_call_id` is end-to-end.** Anthropic and OpenAI both correlate
   `tool_use` blocks with `tool_result` replies by id.
3. **Provider-specific knobs stay inside the provider.** Ollama's
   `think`/`num_ctx`/`num_gpu`/host, Anthropic's `max_tokens`, OpenAI's
   `stream_options` — each adapter's `__init__` reads its own settings.

### Tool schema as JSON Schema, wrapped per provider

Each tool file now exposes `NAME` / `DESCRIPTION` / `PARAMETERS` (the
inner JSON Schema). The registry's `Tool` dataclass carries those plus
`fn` + `requires_approval`. Three formatters in
[backend/app/tools/registry.py](../../backend/app/tools/registry.py)
wrap the canonical shape:

- `ollama_tool_specs(tools)` → `{"type": "function", "function": {...}}`
- `openai_tool_specs(tools)` → same (OpenAI's "function" tools)
- `anthropic_tool_specs(tools)` → `{"name", "description", "input_schema"}`

Each provider calls its own formatter internally; the loop passes a
canonical `list[Tool]` and stays format-agnostic.

### Selection at runtime

`PA_LLM_PROVIDER` ∈ `{ollama, anthropic, openai}` (default `ollama`).
`backend/app/llm/__init__.get_provider()` is the factory; imports are
lazy so an unused backend's SDK doesn't have to load. New config knobs:

| Var | Default |
|---|---|
| `PA_LLM_PROVIDER` | `ollama` |
| `PA_ANTHROPIC_API_KEY` | — |
| `PA_ANTHROPIC_MODEL` | `claude-haiku-4-5` |
| `PA_ANTHROPIC_MAX_TOKENS` | `4096` |
| `PA_OPENAI_API_KEY` | — |
| `PA_OPENAI_MODEL` | `gpt-4o-mini` |

`PA_OLLAMA_*` keep their meaning, but only `OllamaProvider` reads them.

## Why these pieces, and what we rejected

### Hand-rolled Protocol over LiteLLM / similar

LiteLLM normalizes every provider behind a single OpenAI-shaped client.
It would have solved this in one `uv add`. We rejected it for the same
reason ADR 0003 §3 rejected smolagents / LangChain — it defeats the
learning goal. Writing the per-provider translation by hand made the
streaming-event differences visible (Anthropic's content-block framing
vs OpenAI's indexed fragments vs Ollama's single-shot tool_calls); that
visibility is the point.

The compatibility cost is ~250 lines of adapter code total. The Anthropic
adapter is the only meaningfully different one; the OpenAI adapter is
mostly schema rewrapping. Cheap enough.

### A normalized chunk type, not "OpenAI as canonical"

A simpler design was to standardize on OpenAI's chunk shape internally
and let the Ollama / Anthropic adapters emit OpenAI-shaped chunks. We
rejected it because (a) Anthropic's stream events don't map cleanly onto
OpenAI chunks without losing information, and (b) we wanted the loop's
contract to be visible in our own types, not in whatever shape OpenAI
ships next year.

### `LLMMessage` instead of "everyone uses dicts"

The previous code used `list[dict[str, str]]` for messages. Once tool
calls and tool_call_ids enter the picture, the dict shape diverges per
provider (Anthropic content blocks vs OpenAI string-encoded arguments vs
Ollama's pydantic ToolCall model). A small dataclass kept the loop
working in one shape and pushed the provider-specific translation into
the adapters. The cost is a few obvious constructor calls in
[backend/app/api/chat.py](../../backend/app/api/chat.py); the gain is
type-checkable code at the call sites that matter.

### `chat_stream` as the only method, not also `chat`

The non-streaming `POST /chat` route now collects the stream into a
single string. One method per adapter; the streaming-vs-batch concern
lives at the call site. Two methods would mean every adapter implements
two paths with subtly different behaviour — exactly the kind of seam
that drifts.

## Consequences

**Easier**

- Switching backends is a one-line env change. Local dev on Ollama, A/B
  testing on Anthropic or OpenAI, no code edits.
- Adding Gemini later is a new adapter + a new formatter + one match arm
  in `get_provider()`. The Protocol shape doesn't change.
- The hosting plan unblocks: the FastAPI app on a cheap VPS can use a
  hosted LLM API without any per-deploy code edits.
- The loop unit-tests no longer import `ollama` — `FakeProvider` yields
  `LLMChunk` directly. Each adapter has its own translation tests with
  `SimpleNamespace` event fakes.

**Harder**

- Three SDKs in the dependency tree now (`ollama`, `anthropic`, `openai`)
  even if only one is used at a time. Each is small (Python SDKs, not
  bundled binaries), so the cost is modest. Lazy imports in
  `get_provider()` keep the unused ones from running their import-time
  code.
- The Anthropic adapter has more translation surface than the others —
  system pulled into a separate kwarg, content-block construction for
  assistant turns, folding consecutive tool messages into a single user
  turn with multiple `tool_result` blocks. Bugs are likely to show up
  here first.
- Cloud usage isn't free. Smoke-testing Anthropic / OpenAI now costs
  pennies per run. Tests don't hit the network — that's deliberate.

## Alternatives reconsidered

- **Ollama-only with cloud models via Ollama's `/api/chat` proxy.**
  Some cloud models are exposed via Ollama-compatible endpoints
  (Groq, etc.) but most aren't (Anthropic isn't). And the wire-shape
  would still be Ollama's, losing native features (Anthropic's
  citations, OpenAI's `o1` reasoning). Rejected — we lose more than
  we save.
- **Pluggable transports without normalized types.** A `Callable` that
  takes raw messages and yields raw chunks would have been even
  smaller code, but every chunk consumer would then need to know which
  provider it's talking to. The point of the abstraction is the
  opposite — push that branching into one place.

## Open questions / risks

- **Anthropic's content-block invariants.** Empty text blocks are
  rejected, user/assistant must alternate, multiple consecutive tool
  results have to fold into one user turn. The adapter handles each but
  the regression surface is wide. The unit tests cover the obvious
  cases; more exotic conversation shapes (e.g. tool_calls in turn 1 then
  a text-only follow-up in turn 2 without tools) may surface later.
- **`duration_ns` for cloud providers.** Ollama reports it directly; the
  Anthropic and OpenAI adapters measure wall-clock around the stream.
  Approximate but good enough for the per-turn tokens/sec figure.
- **`PA_OLLAMA_*` config is now misleading when `PA_LLM_PROVIDER!=ollama`.**
  The vars still exist and are read by `OllamaProvider` only — they
  silently do nothing for other providers. Acceptable; the alternative
  (renaming or namespacing the existing vars) would break local `.env`
  files. We may revisit at Phase 6 cleanup.

## What this doesn't change

- The ReAct loop shape from ADR 0003 — sequential, one model call per
  iteration, JSON-tool-call protocol. The loop body got *narrower*
  (provider knobs left), not different.
- The WS frame protocol from ADRs 0002 / 0004. Tokens, tool_call,
  tool_result, tool_approval, done frames are unchanged.
- The streaming-with-tools invariants from ADR 0004. Streaming still
  happens every iteration; tool_calls still finalize on the last chunk.
  The Anthropic and OpenAI adapters are responsible for upholding that
  invariant on their respective protocols.
