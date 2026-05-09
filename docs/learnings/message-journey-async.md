# How a chat message flows end-to-end (async)

What we learned tracing one user message from the React composer through to the model and back on 2026-05-10. Phase 2-era code; the loop streams every iteration per [ADR 0004](../decisions/0004-streaming-with-tools.md). Companion to [agent-loop-architecture.md](agent-loop-architecture.md) and [streaming-with-tools.md](streaming-with-tools.md), which dig into the loop itself; this doc focuses on the async/concurrency shape of the surrounding pipeline.

## The headline

The whole pipeline is **one asyncio event loop, one coroutine per WebSocket connection, sequential per turn**. There is no thread pool in the hot path: Ollama is reached over HTTP via `httpx.AsyncClient` (non-blocking), tools are `async def`, and the in-memory buffer is plain dict mutation. "Async" in this codebase means cooperative multitasking on a single thread — every `await` is a yield point where the event loop can serve other connections.

That property is what makes the rest of the design work. Everything else in this doc is a consequence.

## The hops

1. **Browser → WebSocket** ([App.tsx:99-124](../../frontend/src/App.tsx#L99-L124)). One `WebSocket('/chat/stream')` per page mount; Vite proxies to `:8000`. The browser WS API is event-driven — `onmessage` fires; React state updates happen inside the callback. There is no `await` on the frontend; the only synchronization primitive is `busy=true` disabling the composer.

2. **uvicorn → FastAPI** ([chat.py:96](../../backend/app/api/chat.py#L96)). `chat_stream(ws)` is one coroutine per connection running `while True: payload = await ws.receive_json()`. Multiple connections = multiple coroutines, all on the same loop.

3. **Build prompt** (sync, [chat.py:46-51](../../backend/app/api/chat.py#L46-L51)). Reads `SOUL.md`-derived system prompt + ring-buffer history + new user message. No `await` because everything is in-process.

4. **Hand off to the agent loop** ([chat.py:160-166](../../backend/app/api/chat.py#L160-L166)). `run_turn` is given two async callbacks — `on_event` (fire-and-forget frame to client) and `request_approval` (round-trip). This indirection ([loop.py:24-25](../../backend/app/agent/loop.py#L24-L25)) keeps the loop transport-agnostic and unit-testable with plain async fakes.

5. **Streaming Ollama call** ([loop.py:103-125](../../backend/app/agent/loop.py#L103-L125)). `await client.chat(stream=True, ...)` returns an async generator; `async for chunk in stream:` iterates. Each iteration is a yield point. Content deltas are forwarded immediately as `token` frames; `tool_calls` arrive in one terminal chunk (see [streaming-with-tools.md](streaming-with-tools.md)).

6. **Tool dispatch** ([loop.py:150-196](../../backend/app/agent/loop.py#L150-L196)). Sequential per turn (ADR 0003). For each call: emit `tool_call` frame → request approval if needed → `await tool.fn(**args)` → emit `tool_result` frame → append `{"role":"tool",...}` to `msgs`.

7. **Loop exit** when a streamed iteration ends with no `tool_calls` ([loop.py:130](../../backend/app/agent/loop.py#L130)). The accumulated content is the final answer. The handler persists, sends `done`, loops back to `await ws.receive_json()`.

## The approval-round-trip trick (the subtlest piece)

When a tool requires approval, the agent loop calls `await request_approval(...)`. The WS handler implements that callback as ([chat.py:135-156](../../backend/app/api/chat.py#L135-L156)):

```python
async def request_approval(call_id, name, args) -> bool:
    await ws.send_json({"type": "tool_approval", ...})
    while True:
        frame = await ws.receive_json()         # reuses the SAME recv channel
        if frame.get("type") == "approval_response" and ...:
            return bool(frame.get("approved"))
```

The same `ws.receive_json()` that started the turn is **borrowed** mid-loop to read the approval. The outer turn-receive loop is paused — no queue, no shared state, no second task.

This works because the protocol enforces "during a turn, the only client→server frame is `approval_response`." The textarea is disabled client-side ([App.tsx:321](../../frontend/src/App.tsx#L321)); the server logs and discards anything else ([chat.py:154-156](../../backend/app/api/chat.py#L154-L156)). So there's a single owner of the socket at every instant.

The cost: this design **assumes one in-flight turn per connection**. If we ever pipeline turns, or have multiple parallel tool calls each with their own approval, we'd need a real `dict[call_id, asyncio.Future]` keyed queue and a single recv-pump task feeding it. Today's simpler shape is enough.

## "Ring buffer" — what we actually mean

The conversation memory ([buffer.py](../../backend/app/memory/buffer.py)) calls itself a ring buffer because of:

```python
d = self._convos.setdefault(conversation_id, deque(maxlen=self._maxlen))
```

A `collections.deque(maxlen=N)` has **ring-buffer semantics**: at capacity, `append` to one end auto-evicts from the other. Bounded FIFO with silent eviction — no manual pruning, no unbounded growth, no ever-expanding prompt.

**Pedantic caveat:** a *true* ring buffer is a fixed-size array with head/tail indices that wrap around. CPython's `deque` is a doubly-linked list of fixed-size blocks. The *behavior* is ring-buffer; the *implementation* isn't. We use the term for the contract — bounded short-term memory — not the data structure. (This matters if anyone ever profiles the buffer and asks "why is this not just an array?" — the answer is "Python doesn't ship one, and at N=32 nobody cares.")

Why this is fine for now: short-term memory's only job is to give the model the last few turns. Phase 3's SQLite + embeddings is the long-term store; old turns disappearing here is a feature.

## No locks on the buffer

The buffer is touched from inside coroutines (`_build_messages`, `_persist_turn`) but uses plain `dict.setdefault` and `deque.append` — no `asyncio.Lock`, no `threading.Lock`. This is safe **only** because:

- We're on a single-threaded event loop.
- Every read/write is between `await` points (no `await` happens inside `append`/`history`/`clear`).
- Therefore no other coroutine can interleave a buffer mutation.

The day we run a buffer write across an `await` (e.g. async embeddings on insert), we'd need to think about this. The day we ever go multi-threaded for any reason, we'd need a lock or a per-loop confinement rule.

## Things that would break this model

Worth keeping in mind as Phase 2+ tools land:

- **A blocking tool.** A `python_exec` that runs CPU work synchronously freezes *every* connection's streaming, because there's only one thread. Wrap with `asyncio.to_thread(...)` or run in a subprocess. Same for any sync I/O lib without an async counterpart.
- **Concurrent turns on one connection.** The "borrow the recv loop for approval" trick only works because at most one turn is in flight per WS. Pipelining requires a real recv-pump + per-call approval future map.
- **Parallel tool calls in one model turn.** Today they're dispatched sequentially ([loop.py:150](../../backend/app/agent/loop.py#L150)). When we want parallelism: `asyncio.gather` over `_dispatch_tool`. But approval prompts would race in the UI — we'd want serialized approval even with parallel execution, which means `asyncio.gather` on the *execution* part and a serialized `request_approval` queue.
- **Mid-stream tool_calls.** Today we let the stream finish before dispatching, because `tool_calls` arrives terminally on this Ollama+qwen3 combo. If a future model emits the call name early and args later as deltas, [loop.py:121-125](../../backend/app/agent/loop.py#L121-L125)'s "last seen wins" silently drops earlier calls. The comment there flags this.
- **WebSocketDisconnect mid-turn.** Today the disconnect raises out of `await ws.send_json` or `await ws.receive_json`; the agent loop's `await on_event` propagates it; the outer try in `chat_stream` re-raises it. The streaming Ollama call keeps generating tokens into the void until the next `await on_event` raises — wasted compute on a dropped client. Acceptable for now; if it bites, hold the stream open via an `asyncio.Task` we can cancel on disconnect.

## Async cheat-sheet for this codebase

| Surface | Mechanism | Where to look |
|---|---|---|
| WS recv/send | `await ws.receive_json()` / `await ws.send_json()` (Starlette) | [chat.py:119, 124](../../backend/app/api/chat.py#L119) |
| Ollama HTTP | `httpx.AsyncClient` via `ollama.AsyncClient`, shared via `lru_cache` | [chat.py:37-43](../../backend/app/api/chat.py#L37-L43) |
| Streaming chunks | `async for chunk in stream` over an httpx SSE-like body | [loop.py:114](../../backend/app/agent/loop.py#L114) |
| Tool execution | `await tool.fn(**args)` — tools must be `async def` | [loop.py:61](../../backend/app/agent/loop.py#L61), [registry.py:24](../../backend/app/tools/registry.py#L24) |
| Approval | inline `await ws.receive_json()` *inside* the agent-loop call stack | [chat.py:147-156](../../backend/app/api/chat.py#L147-L156) |
| Memory buffer | sync — single-threaded event loop is the lock | [buffer.py](../../backend/app/memory/buffer.py) |
| Browser side | event callbacks (`onmessage`), not async/await | [App.tsx:108](../../frontend/src/App.tsx#L108) |

## Pitfalls worth knowing

- **Tool callables must be `async def`.** [registry.py:24](../../backend/app/tools/registry.py#L24) types them as `Callable[..., Awaitable[str]]`; the loop does `await tool.fn(**args)`. A sync `def` would return a string, and `await "hello"` raises `TypeError: object str can't be used in 'await' expression`. Easy to forget when writing a tool that has nothing to wait on (e.g. local file read on a small file).
- **`await client.chat(stream=True, ...)` returns the iterator instantly; the model hasn't started.** TTFT is time to the first non-empty chunk, not time to `await` returning. Same warning as in [streaming-with-tools.md](streaming-with-tools.md).
- **`AsyncClient` is `lru_cache`'d** ([chat.py:37](../../backend/app/api/chat.py#L37)). One client, shared httpx connection pool. Don't construct a new one per request.
- **`await on_event` applies backpressure.** If the client (or kernel send buffer) stalls, the agent loop blocks on `send_json`, which means we stop reading from Ollama, which means httpx eventually stalls the upstream stream too. In dev this is invisible because everything is local; over a real network it could matter for very long generations.
- **Don't `gather` `on_event` calls.** They look fire-and-forget but they share the WS — concurrent `send_json` on the same socket interleaves bytes and corrupts frames. Sequential `await` is correct.
- **`buffer.append` happens *after* the loop returns** ([chat.py:167](../../backend/app/api/chat.py#L167)). If the loop raises (max steps, repeated tool failure), the user message is **not** persisted. That's intentional — failed turns shouldn't pollute history — but it means a failed turn looks gone from the model's perspective on the next turn.

## Why this matters beyond Phase 2

The single-thread, single-coroutine-per-connection shape is the simplest thing that holds together for an agentic system: tool calls, approval round-trips, streaming, and memory all live in one linear async function we can read top-to-bottom. The day we add voice (Phase 4), web search (Phase 2), or long-term memory retrieval (Phase 3), each new step is one more `await` in the same sequence. We pay the complexity tax only when one of the "things that would break this model" actually breaks it.
