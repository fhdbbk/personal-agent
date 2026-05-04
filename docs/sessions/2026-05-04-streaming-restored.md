# 2026-05-04 — Streaming restored, dead-code trim, two learnings docs

## Goal

Walk Fahad through the Phase 2 agent code as a learning exercise, document the outcomes, and pick off any obvious cleanups exposed by reading the code closely. Streaming-with-tools was an open ADR question; if a five-minute probe showed it was feasible, fix the UX regression today. Otherwise leave it for later.

## What we did

1. **Code walkthrough as Q&A.** Fahad drove a series of targeted questions about [backend/app/agent/loop.py](../../backend/app/agent/loop.py) and [backend/app/tools/registry.py](../../backend/app/tools/registry.py): the `Callable[..., Awaitable[str]]` annotation on the registry's `fn` field, the bare `*` keyword-only marker in `_dispatch_tool`'s signature, and what the `await on_event(...)` call inside the loop actually accomplishes.
2. **Dropped the dead `on_event` parameter from `_dispatch_tool`.** Reading the function body, `on_event` was passed in but never referenced — I'd added it speculatively expecting to emit interim frames during dispatch, which never happened. Removed from the signature and the call site in [loop.py](../../backend/app/agent/loop.py). Re-imported to confirm nothing else was relying on it.
3. **Probed streaming with tools.** Fahad noticed the token-by-token UX from Phase 1 was gone — Phase 2's non-streaming MVP (per ADR 0003 §1's deferred open question) was sending the final answer as a single fat `token` frame. Wrote a small probe script to inspect what Ollama actually streams when `tools=[...]` is passed.
4. **Recovered from a misleading first probe.** Initial run showed 67 chunks of empty content followed by `'Hi'` on chunk 66 — looked exactly like the buffered-content shape we feared. Re-running the same prompt twice produced 3 chunks streaming normally. The 67-chunk run was a cold-model warmup artifact: empty chunks while weights loaded, not content holdback. Same lesson as [prefill-vs-decode.md](../learnings/prefill-vs-decode.md) but in disguise — always probe twice on a warm model.
5. **Confirmed the actual streaming shape.** With `tools=[...]` enabled and `qwen3.5:4b` warm: content streams as deltas chunk-by-chunk; `tool_calls` arrive complete in a single late chunk (not interleaved, not split as deltas). The architecture for handling this is the simplest thing that could work — accumulate content, remember the last `tool_calls`, decide at stream end.
6. **Restructured the loop to stream every iteration.** [loop.py](../../backend/app/agent/loop.py) now runs `stream=True` every iteration. Per chunk, non-empty `msg.content` is forwarded to the WS handler as a `token` frame; the latest `msg.tool_calls` (if any) is recorded. After the stream ends, no `tool_calls` → return content (already streamed); otherwise build the assistant-with-tool_calls dict by hand (since we don't have a single `Message` object) and dispatch. Removed the duplicate final-token send in [api/chat.py](../../backend/app/api/chat.py).
7. **Smoke-tested the full agent flow with streaming.**
   - Plain chat: 8 token frames over the reply (real streaming, not one block).
   - Tool turn (read_file): tool_call → tool_result → 12 streamed tokens after the result.
   - Approval turn (write_file): tool_call → tool_approval → approve → tool_result → 2 streamed tokens.
   Frontend `tsc --noEmit` clean. The protocol is unchanged — UI just sees more `token` frames per turn.
8. **Wrote [ADR 0004](../decisions/0004-streaming-with-tools.md).** "Stream every iteration; tool_calls finalize on the last chunk." Explicitly supersedes only ADR 0003 §1's streaming sub-decision; the rest of 0003 (sequential ReAct, dict registry, sandbox, approval gating) still holds. Updated ADR 0003's status header to reflect the partial supersede. Added the row to [CLAUDE.md](../../CLAUDE.md)'s decisions table.
9. **Captured today's learnings as two topic docs:**
   - [docs/learnings/streaming-with-tools.md](../learnings/streaming-with-tools.md) — the probe story, the cold-warmup dead end, the streamed loop shape, pitfalls (content-as-deltas vs tool_calls-as-final, the `model_dump()` hand-rebuild after retrofitting streaming).
   - [docs/learnings/agent-loop-architecture.md](../learnings/agent-loop-architecture.md) — the *why* behind the architecture: transport-agnostic loop with two callbacks (push `on_event` vs pull `request_approval`), discriminated-union frame protocol, `Callable[..., Awaitable[str]]` and bare-`*` Python explainers folded in where they live in the code, why a dict registry instead of a decorator.

## Decisions made

- [ADR 0004: Streaming with tools](../decisions/0004-streaming-with-tools.md) — every agent iteration streams; `tool_calls` lands whole on the closing chunk; the loop branches at stream end. Supersedes ADR 0003 §1's non-streaming sub-decision.

## Snags + fixes

- **First probe lied.** 67 empty chunks looked like buffered content — exactly the failure mode we'd written ADR 0003 §1 around. Almost stopped there. Always run the probe twice on a warm model; the cold-model envelope is a different statistical population.
- **Retrofitting streaming changed the message-history shape.** Previously we appended `assistant.model_dump(exclude_none=True)` (a single Pydantic Message). With streaming there's no single Message — you build the dict explicitly: `{role:"assistant", content:<accumulated>, tool_calls:[tc.model_dump() for tc in final_tool_calls]}`. Easy to miss when retrofitting; would silently break multi-iteration loops.
- **The duplicate-token bug nearly happened.** After the loop started emitting `token` frames mid-stream, the WS handler was still sending a final `token: <full text>` frame after `run_turn` returned. Caught and removed in the same change; would have rendered every reply twice.

## Open threads / next session

- **Phase 2's second session: `web_search` + `python_exec`.** Still the next phase work. Each pulls in its own decisions: search backend choice (DDG HTML vs SearXNG vs Brave) and `python_exec` sandboxing (`subprocess` + `RLIMIT_CPU` / `RLIMIT_AS` + cwd-pinned). ADR 0003 §5 is the starting point; expect to write a learnings doc on Linux rlimits while we're at it.
- **Unit tests on the agent loop.** The transport-agnostic shape exists specifically so we can fake `on_event` / `request_approval` and a fake Ollama client. Worth doing before the loop grows another condition.
- **Streamed mid-iteration tool_calls deltas.** Today's loop assumes `tool_calls` arrives whole. Comment in [loop.py](../../backend/app/agent/loop.py) flags this; if a future model emits the call name early and args later, last-seen-wins would silently drop earlier ones. Fix when it actually bites.
- **Pre-tool reasoning streaming.** qwen3.5:4b emits zero content before a tool call today, so the "let me check that file…" pattern is supported by the architecture but unverified. May start working automatically when we change models.
- **UI papercuts.**
  - Long tool args / results — the `<pre>` blocks have `max-height:200px` but worth eyeballing in a real browser.
  - "Running…" status is just text. Consider a subtle indicator for the active tool card while the model is generating its post-tool reply.
- **Carried forward (still pending, on Fahad):**
  - Phone browser smoke test — now also covers the streaming + tool transcript flow.
  - Disable Windows Ollama autostart.
  - DHCP reservation for the laptop.
  - SOUL.md rewrite in own voice.

## Out-of-band notes

- ADR 0003's open question on approval UX shape (modal vs inline) is effectively settled by use: inline transcript cards work, the user can see the tool args before approving, and there's no scroll-away-and-miss problem yet because the composer is disabled while a turn is in flight. Will revisit only if real use surfaces a "I missed it" complaint.
