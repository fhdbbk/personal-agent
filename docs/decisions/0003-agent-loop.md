# 0003 — Agent loop: native tool-calling, sequential ReAct, sandboxed tools

**Status:** Accepted (§1's streaming sub-decision superseded by [ADR 0004](0004-streaming-with-tools.md))
**Date:** 2026-05-03
**Phase:** 2

## Context

Phase 2 turns the chat MVP into an agent: the model can decide to call tools instead of (or before) replying, and the loop feeds tool results back into the model until it produces a final answer. CLAUDE.md commits us to a hand-built loop — no LangChain/LangGraph/smolagents — and the first toolset is `read_file`, `write_file`, `web_search`, `python_exec`.

Five things to pin before writing code:

1. **Tool-call format on the wire to Ollama** — native structured `tool_calls` vs prompt-and-parse JSON.
2. **Loop shape** — single tool per turn (sequential) vs parallel tool calls.
3. **Tool registry shape** — how a tool is declared and dispatched.
4. **Browser-visible protocol** — what new frame types extend ADR 0002.
5. **Safety boundary** — what's sandboxed and where approval is required.

## Decision

### 1. Native tool-calling (not prompt-and-parse)

Ollama's `chat(tools=[...])` returns structured `tool_calls` on `response.message.tool_calls`. Qwen3 is trained for this format.

- Reliable: no regexing JSON out of prose, no recovery from "I'll call the tool now: ```json...".
- The educational lift is the **loop**, not the parser. Hand-building "find the JSON block in the model's output" is a busywork tax, not a learning moment.
- Open question deferred: streaming + tool-calls. Ollama may not stream tool_calls cleanly. Phase 2 MVP issues a non-streaming call when the model can use tools, falls back to the streaming code path only for the final no-tool answer turn. Revisit when the UX papercut justifies the complexity.

### 2. Sequential, ReAct-style loop

```
loop:
  resp = ollama.chat(history + tools)
  if resp has no tool_calls:
      return resp.content                 # final answer
  for each tool_call in resp.tool_calls:
      result = dispatch(tool_call)        # sequential, even if model emitted N
      history.append(tool_result)
  if step_count > MAX_STEPS: raise
```

- **One tool at a time, even when the model requests a batch.** Phase 2 priority is observability and learning the dispatch path; parallelism makes interleaving, partial-failure, and approval flows messier than they need to be on day one.
- **`MAX_STEPS = 8`** (env: `PA_AGENT_MAX_STEPS`). Hitting the cap raises and the turn fails — better than letting a confused model spin.
- **Per-tool timeout = `PA_REQUEST_TIMEOUT_S`**. Per-turn ceiling is implicit: `MAX_STEPS × timeout`.
- **On tool error, feed the error string back to the model as the tool result** for up to 2 retries on the same tool. Lets the model self-correct (wrong path, malformed args). After that, the turn fails with the last error.

### 3. Tool registry: a dict, not a framework

```python
# backend/app/tools/registry.py
TOOLS: dict[str, Tool] = {
    "read_file":   Tool(fn=read_file,   schema={...}, requires_approval=False),
    "write_file":  Tool(fn=write_file,  schema={...}, requires_approval=True),
    "web_search":  Tool(fn=web_search,  schema={...}, requires_approval=False),
    "python_exec": Tool(fn=python_exec, schema={...}, requires_approval=True),
}
```

- A `Tool` is a dataclass: callable, JSON-schema (the same shape Ollama wants under `tools=[{type:"function",function:{...}}]`), and an approval flag.
- No decorator magic, no auto-discovery. The dict is the contract; adding a tool is one entry.
- Tools are async. Sync tools wrap themselves with `asyncio.to_thread`.

### 4. Wire protocol — extend ADR 0002

New server → client frames on `/chat/stream`:

```json
{"type": "tool_call",       "name": "read_file", "args": {...}, "call_id": "..."}
{"type": "tool_result",     "call_id": "...",    "ok": true,   "preview": "..."}
{"type": "tool_approval",   "call_id": "...",    "name": "...", "args": {...}}
```

New client → server frames:

```json
{"type": "approval_response", "call_id": "...", "approved": true}
```

- `tool_call` and `tool_result` are **transcript frames** — the UI renders them so the user can see what the agent did. `preview` is a truncated string (≤ 500 chars) of the tool's output; the full result still goes back to the model.
- `tool_approval` is a **request from server to client** — the loop pauses on a future and resumes when the matching `approval_response` arrives. This is the upgrade path ADR 0002 anticipated.
- Existing `token` / `done` / `error` frames are unchanged. The `done` frame closes the *whole turn*, not each model call inside it.

### 5. Safety boundary

- **`read_file` / `write_file`** are scoped to a single sandbox dir (`PA_AGENT_SANDBOX`, default `./sandbox/`). All paths are resolved relative to the sandbox; symlinks and `..` traversal are rejected.
- **`write_file`** requires user approval per call (Phase 2 default). Override via `PA_AGENT_AUTO_APPROVE=true` for dev.
- **`python_exec`** runs in a subprocess (`uv run python -c "…"` or similar) with:
  - CPU-time limit (`resource.setrlimit(RLIMIT_CPU)`),
  - memory limit (`RLIMIT_AS`),
  - no network (parent process drops `CAP_NET_*` via `unshare` on Linux, or — easier first cut — runs with `HTTP_PROXY=` set to an unreachable address and trusts that the model isn't trying to escape),
  - no FS access outside the sandbox dir (chdir + the sandbox is the only writable path),
  - approval required per call.
- **`web_search`** uses a single configured search backend (DuckDuckGo HTML scrape for the MVP — no API key, easy to swap). No approval; results are read-only.

The hard sandbox is deliberately a "good enough" cut. A determined model can probably escape `python_exec` on the first version. The point of the boundary is to make accidental damage impossible, not to defend against an adversarial model — that's a Phase 6+ problem if it ever becomes one.

## Consequences

**Easier**
- The loop is ~80 lines of clearly readable Python: dispatch table + while loop + history append. Exactly the kind of thing the learning project was set up for.
- The wire protocol is a strict superset of Phase 1's; the React UI keeps working, and tool transcript rendering is purely additive.
- Approval flows fall out of the WS bidirectionality decided in ADR 0002 — no new transport.

**Harder**
- We own MAX_STEPS, retry policy, malformed-args recovery, sandbox correctness. Each is a place a framework would have given us a pre-baked answer.
- Mixing tool calls with token streaming is awkward: the MVP non-streams when tools are available, which means cold-start TTFT for a tool-using turn is whatever Ollama takes to produce the *whole* tool_calls response. Acceptable; revisit when it hurts.
- `python_exec` sandboxing is the riskiest piece. We commit to "subprocess + rlimits + sandbox dir" for the MVP and explicitly accept that it's not adversary-proof.

## Alternatives considered

- **Prompt-and-parse JSON tool calls.** Rejected — see §1. Educational value is in the loop, not the regex.
- **Parallel tool calls in a single step.** Rejected for Phase 2 — sequencing is simpler to reason about, debug, and approve. Revisit once we have a tool that's I/O-bound and parallel-friendly (multiple `web_search` queries).
- **Smolagents / LangGraph.** Rejected by ADR 0001; reaffirmed here.
- **Docker container for `python_exec`.** Rejected for MVP — adds a Docker dependency and ~1-2 s startup cost per call, both heavy for a local-first laptop project. `subprocess + rlimits` is the leaner first cut.
- **Approval-by-default on every tool.** Rejected — would make `read_file` and `web_search` feel like nagware. Mutating/executing tools (`write_file`, `python_exec`) approve; read-only tools don't.

## Open questions for Phase 2 implementation

- Does Ollama's streaming API surface partial `tool_calls` mid-stream, or only at the end? (Determines whether we can stream the assistant's pre-tool reasoning.)
- DuckDuckGo HTML scrape is fragile. If it breaks, fall back to SearXNG (self-hosted) or Brave Search API (free tier, key required). Decide if/when it breaks.
- Approval UX: modal? inline transcript card with Approve/Deny buttons? Defer to implementation.
