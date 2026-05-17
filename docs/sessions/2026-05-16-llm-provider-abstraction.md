# 2026-05-16 — LLM provider abstraction (Ollama + Anthropic + OpenAI)

## Goal

Make the agent loop and chat endpoint backend-agnostic so we can run on
Claude Haiku or `gpt-4o-mini` (cloud APIs) without abandoning local
Ollama. This was originally going to be one half of a hosting plan, but
Fahad split the work: ship the provider seam now, defer the VPS + domain
+ TLS + auth plan to a future session.

The earlier conversation drafted a [hosting plan](/home/fahad/.claude/plans/what-if-we-have-swirling-origami.md)
and then rescoped to just the abstraction. Three providers in this pass:
Ollama (the existing default), Anthropic (most architecturally different
— content-block tool protocol), OpenAI (closest to Ollama; easy port).

## What we did

1. **Normalized types + Protocol** ([backend/app/llm/base.py](../../backend/app/llm/base.py)).
   Five dataclasses (`LLMMessage`, `LLMToolCall`, `LLMUsage`, `LLMChunk`)
   + one `LLMProvider` Protocol — ~70 lines, no framework. Three
   invariants the adapters hold: tool calls only emitted complete,
   `tool_call_id` end-to-end, provider-specific knobs stay inside the
   provider.
2. **Tool registry restructure** ([backend/app/tools/registry.py](../../backend/app/tools/registry.py)).
   Each tool file now exposes `NAME` / `DESCRIPTION` / `PARAMETERS` (the
   inner JSON Schema) instead of a full Ollama-shaped `SCHEMA` dict. The
   `Tool` dataclass carries those plus `fn` + `requires_approval`. Three
   formatters wrap the canonical shape per provider: `ollama_tool_specs`,
   `openai_tool_specs` (wire-identical to Ollama's "function" tools),
   `anthropic_tool_specs` (`{name, description, input_schema}`).
3. **OllamaProvider** ([backend/app/llm/ollama.py](../../backend/app/llm/ollama.py)).
   Wraps `ollama.AsyncClient`. Synthesizes `tc_<n>` ids for tool calls
   since Ollama doesn't natively use them. Reads `PA_OLLAMA_*` settings
   internally so the loop signature stays narrow.
4. **AnthropicProvider** ([backend/app/llm/anthropic.py](../../backend/app/llm/anthropic.py)).
   Pulls `system` out of the message list into a separate kwarg, builds
   content blocks for assistant turns (`text` + `tool_use`), folds
   consecutive tool messages into a single user turn with multiple
   `tool_result` blocks. Consumes the SDK's event stream: `text_delta`
   passes through directly; `input_json_delta` fragments accumulate per
   content-block index and parse to JSON at `content_block_stop`.
5. **OpenAIProvider** ([backend/app/llm/openai.py](../../backend/app/llm/openai.py)).
   Wire shape is essentially Ollama's. Tool-call arguments arrive as
   JSON-string fragments keyed by `index`; the adapter concatenates per
   index then parses. Uses `stream_options={"include_usage": True}` so
   usage lands on the trailing chunk after `choices=[]`.
6. **Loop refactor** ([backend/app/agent/loop.py](../../backend/app/agent/loop.py)).
   `client: AsyncClient` → `provider: LLMProvider`. Base messages now
   `list[LLMMessage]`. The streaming reader consumes `chunk.delta_text`
   / `chunk.tool_calls` / `chunk.usage` instead of poking at Ollama's
   `chunk.message.content` / `eval_count`. Tool-result messages thread
   `tool_call_id` through so Anthropic/OpenAI can correlate.
7. **Chat endpoint** ([backend/app/api/chat.py](../../backend/app/api/chat.py)).
   `_client()` → `_provider()` via the new `get_provider()` factory.
   The non-streaming `POST /chat` now routes through `run_turn` with
   no-op callbacks (collect the stream into a string) instead of calling
   the SDK directly — one method per adapter, one code path.
8. **Config** ([backend/app/config.py](../../backend/app/config.py)).
   New `PA_LLM_PROVIDER` (default `ollama`), `PA_ANTHROPIC_*` (key /
   model / max_tokens), `PA_OPENAI_*` (key / model). The existing
   `PA_OLLAMA_*` knobs keep their semantics — only `OllamaProvider`
   reads them.
9. **Tests, four files, 27 passing, zero live LLM calls.**
   - [test_agent_loop.py](../../backend/tests/test_agent_loop.py) ported
     from `FakeClient` (Ollama types) to `FakeProvider` (yields
     `LLMChunk` directly). All 11 prior tests stay structurally
     identical; one (`test_provider_receives_messages_and_tools_per_iteration`)
     rewritten because the provider hides options/think/model now.
   - [test_provider_ollama.py](../../backend/tests/test_provider_ollama.py)
     — 4 tests covering OllamaProvider's chunk translation + id
     synthesis.
   - [test_provider_anthropic.py](../../backend/tests/test_provider_anthropic.py)
     — 7 tests covering message folding (system extraction, tool-result
     folding, empty-text omission) and event-stream consumption
     (text deltas, JSON-fragment assembly, tool schema wrapping).
   - [test_provider_openai.py](../../backend/tests/test_provider_openai.py)
     — 5 tests covering message shape, fragment assembly, parallel tool
     calls indexed correctly, tool schema, stream_options threading.
10. **Smoke scripts.** The WS-driven smokes (`smoke_agent.py`,
    `smoke_agent_web_search.py`, `smoke_agent_fetch_url.py`) don't talk
    to Ollama directly — they hit the server — so they work against
    whatever provider is configured without changes. Added
    [scripts/smoke_provider.py](../../scripts/smoke_provider.py) — a
    provider-agnostic one-shot ping for verifying credentials and
    connectivity. End-to-end against the live local Ollama returns
    `pong` after 16 s (cold load).
11. **ADR 0007** ([docs/decisions/0007-llm-provider-abstraction.md](../decisions/0007-llm-provider-abstraction.md)).
    Records the decision, the alternatives rejected (LiteLLM,
    OpenAI-shape-as-canonical, raw dicts), and the open risks
    (Anthropic's content-block invariants, approximate `duration_ns`
    for cloud providers).
12. **CLAUDE.md** updated — new ADR row in the decisions table, new
    `backend/app/llm/` and per-provider test files in the repo layout,
    new `PA_LLM_PROVIDER` / Anthropic / OpenAI config sections, new
    `smoke_provider.py` in commands + layout.
13. **Design docs refreshed** ([HLD.md](../design/HLD.md), [LLD.md](../design/LLD.md)).
    HLD §2 system-context diagram gains the cloud-API edges (dashed);
    §3 component view adds the `llm/` subgraph; §4 tech-stack table
    gets rows for ADRs 0005/0006/0007; §6 cross-cutting concerns gets
    an "LLM provider selection" paragraph; §9 repo layout shows
    `backend/app/llm/`. LLD gets a new top-level §7 "LLM providers"
    section (translation tables for the three providers + per-adapter
    event/chunk handling + an Anthropic sequence diagram), §§8–18
    renumbered, §2 data-structures table grows to include the four new
    `LLM*` types and the new `Tool` shape, §3 API-layer text rewritten
    around `_provider()` instead of `_client()`, §5 loop pseudocode +
    bullets + sequence diagrams now provider-agnostic
    (`participant LLM as LLMProvider` instead of `participant Ollama`),
    §6 tool registry shows the three per-provider formatters, §11
    configuration table reorganised into LLM-selection / Ollama /
    Anthropic / OpenAI / Agent groups, §18 appendix updated with new
    file paths + line counts. Stale `chat.py:NNN` line anchors fixed
    after the chat-endpoint refactor shifted them.

## Decisions made

- **Hand-roll the abstraction, don't use LiteLLM.** Same reason ADR 0003
  rejected smolagents — using a normalizer-library would defeat the
  learning goal. The translation work is what surfaces how the providers
  actually differ (Anthropic's content blocks vs OpenAI's indexed
  fragments vs Ollama's all-at-once tool_calls). 250-odd lines total
  across the three adapters; cheap enough.
- **Normalized chunk type, not "OpenAI as the canonical shape."**
  Forcing the Anthropic and Ollama adapters to emit OpenAI-shaped
  chunks would lose information (content-block ids, Ollama's
  eval_duration). A small dataclass is the right pivot point — it owns
  the contract our loop depends on.
- **Tool schema canonicalises to JSON Schema, providers wrap.** Each
  tool file exposes its inner `parameters` schema; the registry
  formatters wrap to per-provider shapes. Easier than maintaining three
  copies of every schema, doesn't introduce a translator framework.
- **`LLMMessage` over `list[dict]`.** Once `tool_call_id` and content
  blocks enter the picture, the dict shape diverges per provider. A
  dataclass keeps the loop in one shape and pushes the translation into
  adapters. The cost is a few `LLMMessage(...)` constructor calls in
  chat.py.
- **Streaming-only adapters; non-streaming routes collect chunks.**
  One method per adapter. The non-streaming `POST /chat` was reimplemented
  via `run_turn` (no-op callbacks, deny approvals) so we don't carry two
  divergent code paths.
- **Lazy imports inside `get_provider()`.** An unused backend's SDK
  doesn't pay any import-time cost. Three providers, ~58 packages
  installed, only one in memory per run.

## Snags + fixes

- **Async-generator return shape, twice.** First, I sketched the
  `FakeProvider.chat_stream` as `async def ... return gen()` — wrong,
  the loop uses `async for chunk in provider.chat_stream(...)` directly,
  so `chat_stream` itself must be an async generator function (use
  `yield`, no `return`). Wrote a long apologetic comment block trying to
  document the trap, then realized the comment block was the trap and
  cleaned it up to a one-line docstring instead. Real adapters
  ([backend/app/llm/ollama.py](../../backend/app/llm/ollama.py)) follow
  the same shape so the test fake matches reality.
- **Symlink editing.** Tried to `Edit` `CLAUDE.md` directly and hit
  "refusing to write through symlink" — `CLAUDE.md` → `AI.md`. Edited
  `AI.md` instead. The 2026-05-16 morning session already noted this in
  its snag list, so reading the prior session log first would have
  saved one round trip. (Reminder to self: actually follow the "read
  the latest session log first" rule from CLAUDE.md.)
- **`extra="ignore"` on Settings saved me.** Adding `PA_LLM_PROVIDER` /
  Anthropic / OpenAI vars without breaking the existing `.env` worked
  because pydantic-settings is configured with `extra="ignore"`. Worth
  remembering: extending Settings is always cheap, removing a field is
  the load-bearing case.

## Open threads / next session

- **Hosting plan.** The deferred half of today's work. The plan file at
  `~/.claude/plans/what-if-we-have-swirling-origami.md` had two halves
  — abstraction (done) and hosting (Hetzner CX22 + Cloudflare Registrar
  + Caddy + HTTP Basic + Anthropic Haiku). Pick up there when ready.
  Now that the abstraction ships, the hosting plan compresses to
  "VPS + reverse proxy + auth + .env knobs", no app changes required.
- **Cloud-provider live smoke.** No live API call has been made against
  Anthropic or OpenAI yet — unit tests cover the translation, but the
  first end-to-end run will probably surface something (model name
  alias, header, billing). `scripts/smoke_provider.py` is ready for it
  the moment a key lands in `.env`.
- **Anthropic conversation-shape regressions.** The adapter handles the
  obvious invariants (system extraction, tool-result folding, empty
  text omission) but more exotic conversation shapes may surface. The
  failure mode is usually a 400 from the API; first one tells us what
  to fix.
- **Per-call `duration_ns` for cloud providers is wall-clock**, which
  includes network. Ollama's is true decode time. The tokens/sec figure
  is therefore not directly comparable across providers — fine for now,
  worth a note if we ever build a comparison view.
- **`python_exec`** — still the last Phase 2 tool. The provider seam
  doesn't change that work; the only relevant question is which
  provider you'd use to develop it against (Anthropic Haiku is probably
  the lowest-friction once a key is in place).
- **rlimits learning doc** — still outstanding.
- **CPU-inference is intentional** — already a memory, sticks for this
  session too. Don't propose moving Ollama off CPU.
