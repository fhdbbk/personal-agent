# 2026-05-03 (evening) — Phase 2: agent loop + first tools shipped

## Goal

Implement the Phase 2 design from [ADR 0003](../decisions/0003-agent-loop.md): a hand-built ReAct loop, two starter tools (`read_file`, `write_file`), the `tool_call` / `tool_result` / `tool_approval` frame additions to the WS protocol, and a UI that renders tool transcript cards with approve/deny buttons. Web search and `python_exec` deliberately deferred — they each pull in their own decisions.

## What we did

1. **Added agent settings** to [backend/app/config.py](../../backend/app/config.py):
   `PA_AGENT_SANDBOX`, `PA_AGENT_MAX_STEPS`, `PA_AGENT_MAX_RETRIES_PER_TOOL`, `PA_AGENT_AUTO_APPROVE`. Kept the existing `PA_REQUEST_TIMEOUT_S` as the per-LLM-call ceiling.
2. **Built the tools layer** under [backend/app/tools/](../../backend/app/tools/):
   - [_sandbox.py](../../backend/app/tools/_sandbox.py) — `safe_path()` resolves a user-supplied path under the sandbox root, then `is_relative_to()`-checks the result against the sandbox real path. Catches `..` traversal, absolute-path overrides, and symlink escape (because `Path.resolve()` follows symlinks before the containment check).
   - [read_file.py](../../backend/app/tools/read_file.py) — async, truncates at 64 KB so a single tool result can't blow out the model's context.
   - [write_file.py](../../backend/app/tools/write_file.py) — async, creates parent dirs, marked `requires_approval=True`.
   - [registry.py](../../backend/app/tools/registry.py) — a frozen `Tool` dataclass and a literal `TOOLS` dict. `ollama_tool_specs()` returns the schemas for `client.chat(tools=...)`. No decorators, no auto-discovery.
3. **Wrote the agent loop** at [backend/app/agent/loop.py](../../backend/app/agent/loop.py). Sequential, non-streaming per ADR §1. Per iteration:
   - call Ollama with `tools=ollama_tool_specs()`,
   - if no `tool_calls` on the response → that's the final answer, return it,
   - otherwise append the assistant message (with tool_calls) and dispatch each tool sequentially, appending a `{role:"tool", content:...}` after each.
   The loop is transport-agnostic — it talks to the WS handler through two callbacks (`on_event`, `request_approval`). Consecutive same-tool failures are tracked; after `MAX_RETRIES_PER_TOOL` the turn raises `AgentError`. Denied approvals are *not* counted as errors (the model gets "User denied this action." back as a tool result and can adapt).
4. **Wired the loop into the WS handler** at [backend/app/api/chat.py](../../backend/app/api/chat.py). The handler builds `base_messages` (system prompt + buffer history + new user turn), defines `on_event` (forwards frames with `conversation_id` injected) and `request_approval` (sends `tool_approval`, blocks on a matching `approval_response`). The final assistant text is shipped as a single `token` frame so the existing UI reducer renders it without needing a new frame type. Updated the docstring to document the full bidirectional protocol.
5. **Frontend transcript model.** [frontend/src/App.tsx](../../frontend/src/App.tsx) — the messages array became a `TranscriptItem[]` discriminated union of `message` and `tool` items. New frame handlers: `tool_call` upserts a tool card, `tool_approval` flips its `awaitingApproval` flag (which paints Approve/Deny buttons), `tool_result` closes it with success/error styling. Added `respondToApproval()` to send the response. New CSS for tool cards in [App.css](../../frontend/src/App.css) — yellow border for awaiting, blue for running, dim green for ok, red for error, with a `<pre>` block for args and result preview.
6. **Smoked end-to-end** with [scripts/smoke_agent.py](../../scripts/smoke_agent.py):
   - "What's in notes.txt? Use read_file" → tool_call(read_file) → tool_result(ok) → assistant repeats the file contents.
   - "Write greeting.txt with 'hello world'" → tool_call(write_file) → tool_approval → approve → tool_result(ok) → "Done." Verified `sandbox/greeting.txt` was actually created.
   - Denial path (separate run): write_file → approval → deny → tool_result("User denied this action.") → model retried once → second denial → model gave up and answered. `blocked.txt` was correctly *not* written.
   - Sandbox unit check: `safe_path()` blocks `../etc/passwd`, `/etc/passwd`, `../../foo`; allows `notes.txt` and `sub/x.txt`.
7. **Docs.** Updated [CLAUDE.md](../../CLAUDE.md) repo layout, commands list, and config reference with the new env vars. Updated [.env.example](../../.env.example) with annotated agent settings. Added `sandbox/` to [.gitignore](../../.gitignore).

## Decisions made

No new ADRs — this session implemented [ADR 0003](../decisions/0003-agent-loop.md). One small implementation choice worth noting: the loop emits `tool_call` *and* (for approval-gated tools) a separate `tool_approval` frame for the same `call_id`. The UI's `upsertTool()` reducer treats the second frame as a patch on the first card. This is layered/additive rather than mutually-exclusive, which keeps the UI state machine simple.

## Snags + fixes

- **Ollama response shape.** First draft used `resp["message"].get("tool_calls")`; the actual `ChatResponse` is a Pydantic model and `Message` doesn't have `.get()`. Switched to `resp.message.tool_calls` (attribute access) and `assistant.model_dump(exclude_none=True)` for re-appending into the message list. A 30-second probe script confirmed the shape before I wrote more code than I had to throw away.
- **Model refused the leading deny prompt.** First denial test said "the user will deny this one"; the model read that and bailed without calling the tool, which meant the deny path never fired. Rephrased neutrally ("Write a file named blocked.txt with 'nope'.") and the model called write_file as expected, after which the deny worked. Worth remembering: small models pick up tone cues that bias them away from the path you actually want to test.
- **Approval-during-turn protocol.** Considered running a parallel WS receive task to multiplex new-message frames against approval frames. Rejected — during a turn the only thing the client should send is `approval_response`, and the UI disables the composer with `busy=true` to enforce that. Saved a chunk of asyncio coordination by trusting the protocol.
- **Streaming sacrifice.** Per ADR, the agent loop is non-streaming throughout. Plain conversational turns now wait for the full reply before the user sees anything, which is a real UX regression vs Phase 1. Acceptable for the MVP; ADR's open question is recorded.

## Open threads / next session

- **Second Phase 2 session: `web_search` + `python_exec`.**
  - Search backend pick: DuckDuckGo HTML scrape (the ADR's MVP choice) vs SearXNG vs Brave API. ADR commits to DDG; first run will tell us how flaky it is.
  - `python_exec` sandboxing: subprocess + `RLIMIT_CPU` + `RLIMIT_AS` + cwd-pinned to sandbox dir. ADR explicitly does not aim for adversary-proof — accidental damage prevention only. Worth a short learnings doc on Linux rlimits while we're at it.
- **Streaming-with-tools as a follow-up.** The non-streaming MVP is fine for tool-using turns but feels slow for plain chat. Two paths to try later: (a) attempt streaming and parse tool_calls from the streamed response; (b) detect "no tools likely needed" upfront and skip `tools=` entirely on those turns. Defer.
- **UI papercuts surfaced by smoke runs but not yet hit by a human.**
  - Long tool args / results may still be ugly; `max-height: 200px` on the result `<pre>` should help but worth eyeballing in a real browser.
  - The "running…" spinner is just text. Fine for now.
- **Unit tests.** None yet. The agent loop is callback-based specifically so we can write a test that fakes the Ollama client + on_event + request_approval. Worth doing before the loop grows more conditions.
- **Carried forward (still pending, on Fahad):**
  - Phone browser smoke test of the new tool transcript UI.
  - Disable Windows Ollama autostart.
  - DHCP reservation for the laptop.
  - SOUL.md rewrite in own voice.
- **Open ADR-0003 questions still unresolved:**
  - Does Ollama stream partial `tool_calls` mid-stream? (Didn't probe — non-streaming made it moot for this session.)
  - Approval UX shape — we landed on inline transcript cards with Approve/Deny. Modal would be more attention-grabbing but interrupts the conversational flow; sticking with inline unless real use shows it's missed.
