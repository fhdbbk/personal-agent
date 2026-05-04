# 0004 — Stream every agent iteration; tool_calls finalize on the last chunk

**Status:** Accepted
**Date:** 2026-05-04
**Phase:** 2
**Supersedes:** [ADR 0003 §1](0003-agent-loop.md) — only the streaming sub-decision; everything else in ADR 0003 still stands.

## Context

ADR 0003 §1 chose native tool-calling and, as an open question, deferred streaming when `tools=[...]` is passed. The Phase 2 MVP shipped non-streaming throughout the agent loop, then sent the final assistant text as a single fat `token` frame for UI compatibility. Plain conversational turns lost the token-by-token UX from Phase 1 — a real regression we said we'd revisit.

We revisited it the same day, after a five-minute probe.

## What the probe found

Driving Ollama 0.22.x with `qwen3.5:4b` and `tools=[read_file, write_file]`:

- **Plain prompt that does not call a tool.** 3 streamed chunks total. Content arrives as deltas on chunks 0–1, `done=true` on chunk 2. `tool_calls` is `None` throughout. Streams the same as a no-tools call.
- **Prompt that calls a tool.** 2 streamed chunks total. Chunk 0 carries `tool_calls=[<one complete call>]` with empty content. Chunk 1 is `done=true`. Tool calls arrive whole in one chunk, not interleaved with content.

So `stream=True, tools=[...]` is not the broken combination ADR 0003 §1 worried about — at least for this model on this Ollama version.

## Decision

The agent loop streams every iteration. Per chunk:

- Forward any non-empty `message.content` to the WS handler as a `token` frame.
- Remember the latest `message.tool_calls`. If it ever arrives split across multiple chunks (it doesn't today), the last seen value wins.

When the stream ends:

- If no `tool_calls` were seen → that text was the final answer (already streamed); persist and emit `done`.
- Otherwise → append the assistant message (`role=assistant`, content = accumulated string, tool_calls = the recorded list, each `model_dump()`-ed for re-serialization) and dispatch.

The WS handler no longer sends a final `token` frame after `run_turn` returns — the loop already streamed it.

## Why not the alternatives

- **Two-call hack** — first a fast tools-enabled non-streamed call to decide whether tools are needed, then a streamed reissue without tools when the answer is conversational. Rejected: doubles the cost of every plain turn just to recover streaming on those turns. The probe makes it unnecessary.
- **Stay non-streaming, mask with a "thinking…" spinner.** Rejected: a spinner doesn't actually reduce latency; tokens-as-they-come is the only thing that makes a slow CPU feel responsive.

## Consequences

**Easier**

- Phase 1's streaming UX is fully back, on every turn — including post-tool reply turns ("here's what was in the file…" streams character-by-character after the tool card closes).
- The frontend's existing `token` frame reducer keeps working unchanged. No new frame type, no new state.

**Harder**

- The loop now manually assembles the assistant message dict (role/content/tool_calls) instead of `assistant.model_dump()`-ing a single Message object. Trivial code, but a place to remember if Ollama ever changes the wire format.
- If a future model or Ollama version splits `tool_calls` across multiple streamed chunks (deltas instead of one complete chunk), the "last one wins" rule will silently drop earlier ones. The code carries a comment flagging this; we'll fix it when it bites.
- Mid-stream `tool_calls` plus simultaneous content is supported by the architecture but unverified — qwen3.5:4b currently emits no pre-tool content, so the "let me check that file…" pattern is theoretical for now.

## What this changes about ADR 0003

Only §1's streaming sub-decision is superseded. The native-tool-calling vs. prompt-and-parse choice still stands, as does the rest of 0003 (sequential ReAct, dict registry, sandboxed tools with approval gating, MAX_STEPS, retry policy).
