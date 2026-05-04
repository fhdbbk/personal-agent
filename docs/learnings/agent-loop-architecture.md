# Agent loop architecture: why the loop knows nothing about WebSockets

What we learned wiring the Phase 2 ReAct loop on [2026-05-03](../sessions/2026-05-03-phase-2-agent-loop.md) and tightening it the next morning. The design choices below are the ones a Python dev coming from synchronous web frameworks will probably ask "wait, why like this?" about — those are the ones worth writing down.

## The headline

Two pillars:

1. **The agent loop is transport-agnostic.** It accepts two callbacks (`on_event`, `request_approval`) and never imports `WebSocket` or `fastapi`. The WS handler is a thin adapter: it gives the loop those two functions, the loop calls them, and the handler turns each call into a JSON frame.
2. **The wire protocol is a discriminated union.** Every server→client and client→server message is a JSON object with a `type` field; consumers switch on `type` to decide what to do. New frame types add capability without breaking older clients (this is exactly what ADRs 0003 and 0004 added on top of the original 0002 protocol).

Both are deliberately *small* abstractions. They give us a unit-test seam and a clean upgrade path without becoming a framework.

## The two callbacks, and why exactly two

```python
OnEvent          = Callable[[dict[str, Any]], Awaitable[None]]
RequestApproval  = Callable[[str, str, dict[str, Any]], Awaitable[bool]]
```

- `on_event(frame)` — **push, fire-and-forget.** The loop sends a frame to the outside world and doesn't expect anything back. Used for `tool_call` (announcing dispatch), `tool_result` (announcing completion), and `token` (streamed assistant text). Returns nothing.
- `request_approval(call_id, name, args)` — **pull, await answer.** The loop hands the caller a question and *waits* for a boolean. Used only for tools with `requires_approval=True`. Returns `bool`.

Why these two and not one general-purpose callback? Because they have different shapes:

- A push callback can be implemented with `ws.send_json(...)` directly. Trivial.
- A pull callback needs the caller to round-trip something — for the WS, it's "send a `tool_approval` frame, then sit on `ws.receive_json()` until the matching `approval_response` comes back." That coordination doesn't make sense to bury inside `on_event` because most events don't need a reply.

Splitting them makes the *types* tell the truth about the protocol. `Awaitable[None]` says "this is a notification"; `Awaitable[bool]` says "this is a question". Future-you reading the loop in six months sees the difference at a glance.

## What the decoupling actually buys us

Three things, in increasing order of how much they matter:

1. **Smaller cognitive surface in `loop.py`.** The file imports `ollama` and `backend.app.tools.registry`. It does not import FastAPI, Starlette, WebSocket, JSON, or any HTTP machinery. When you're reading the loop, the only concepts in the room are messages, tool calls, and dispatching — not connection lifecycles.
2. **Trivial unit testability.** A test passes fakes:
   ```python
   events = []
   async def on_event(f): events.append(f)
   async def request_approval(*a): return True
   await run_turn(..., on_event=on_event, request_approval=request_approval, client=fake_ollama)
   assert events[0]["type"] == "token"
   ```
   No HTTP server, no WS client, no async fixture acrobatics. (We haven't written these tests yet — but we *can*, which was the point.)
3. **Reusable from non-WS drivers.** A CLI tool that prints events to stdout and reads `y/n` from stdin for approvals is a 15-line wrapper around the same loop. Same for a future programmatic API for batch agent runs. None of that requires touching `loop.py`.

The total cost of this decoupling is: two extra parameters and one extra lambda-ish thing in [api/chat.py](../../backend/app/api/chat.py). Cheap.

## The discriminated-union protocol

ADR 0002 picked this for the WS originally. ADRs 0003/0004 extended it. The shape:

```ts
type ServerFrame =
  | { type: 'token',         delta: string,  conversation_id: string }
  | { type: 'tool_call',     call_id, name, args, conversation_id }
  | { type: 'tool_approval', call_id, name, args, conversation_id }
  | { type: 'tool_result',   call_id, ok: boolean, preview, conversation_id }
  | { type: 'done',          conversation_id }
  | { type: 'error',         error: string, conversation_id? }
```

The `type` field is the discriminator: TypeScript narrows the rest of the union once you `if (frame.type === 'tool_call') { ... }`. Same idea on the Python side, except we use plain dicts and check `frame["type"]` directly.

Two things that fall out of this for free:

- **Adding a frame type is additive.** When ADR 0003 needed `tool_call`/`tool_result`/`tool_approval`, no existing frame had to change. Old clients that don't know about the new types ignore them and keep working with the original token/done/error set. (Real lesson: design wire formats so new fields don't break readers.)
- **Errors stay in-band.** The protocol carries `{type: 'error'}` rather than relying on the WS connection itself dropping. The connection survives an error; the client sees a typed message and can keep sending. This is why a retry-able tool failure doesn't drop the chat.

## Why `Callable[..., Awaitable[str]]` for the tool registry

```python
@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., Awaitable[str]]
    schema: dict
    requires_approval: bool
```

Different tools have different signatures: `read_file(path)`, `write_file(path, content)`, eventually `web_search(query, k=5)`. The dataclass field has to hold all of them. The type hint:

- `Callable[X, Y]` — first slot is *parameters*, second is *return*.
- `...` (the literal Ellipsis, not a placeholder) in the parameter slot means "any signature; I don't care what kwargs it takes" — which is what we need because the fields differ across tools. We could write `Callable[[str], Awaitable[str]]` for read_file specifically, but we can't write a single such hint that fits both read_file and write_file.
- `Awaitable[str]` is the return: every tool must be async, and must resolve to a string (the dispatcher feeds that string back to the model). `async def f() -> str` returns a `Coroutine[Any, Any, str]`, which is a subtype of `Awaitable[str]`, so all our tools fit.

Worth knowing: this is a hint for the type-checker, not enforcement. You can put a sync function in `fn` and Python won't complain at construction time — it'll explode at the `await` later, at runtime. The dispatcher does an `isinstance(result, str)` belt-and-suspenders check just in case.

## Why the bare `*` in some signatures

```python
async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str,
    request_approval: RequestApproval,
) -> tuple[bool, str]:
```

The bare `*` is a **keyword-only marker**: every parameter after it must be passed by keyword, never positionally. Two reasons we use it for the orchestration parameters:

- **Readability at the call site.** `(name, args, call_id, request_approval)` as positionals are easy to reorder by accident. Forcing `(name, args, call_id=cid, request_approval=app)` makes the intent unambiguous.
- **Forward-compat seam.** We can add another keyword-only parameter later without breaking any positional caller. (We already used this seam once — when the dead `on_event` parameter came out, no call sites needed updating beyond the one keyword.)

Three syntactic siblings worth keeping straight:

| Syntax | Meaning |
|---|---|
| `*args` | collect positional extras into a tuple |
| `**kwargs` | collect keyword extras into a dict |
| bare `*,` | "no name, just stop accepting positionals here" |

Same family of features, different jobs.

## Why a dict for the tool registry, not a decorator

```python
TOOLS: dict[str, Tool] = {
    "read_file":  Tool(name="read_file",  fn=read_file,  schema=READ_FILE_SCHEMA,  requires_approval=False),
    "write_file": Tool(name="write_file", fn=write_file, schema=WRITE_FILE_SCHEMA, requires_approval=True),
}
```

A `@tool` decorator would let us write:

```python
@tool(name="read_file", requires_approval=False)
async def read_file(path: str) -> str: ...
```

…and have the schema auto-derived from the type hints. Tempting, especially for a learning project where "look how clean!" is its own reward.

We didn't, for three reasons:

1. **The dict is the *contract*.** Reading [registry.py](../../backend/app/tools/registry.py) tells you everything that exists, what each takes, and what's approval-gated, in fifteen lines of code. A decorator scatters that across files.
2. **Auto-derived schemas drift.** The tool's *description* — the prose the model reads to decide whether to call it — matters at least as much as its parameter types. Hand-written schemas force us to write good descriptions on purpose; introspection-derived ones encourage skipping the description because "the function name is descriptive enough." It isn't.
3. **No magic to debug.** When (not if) a model produces a malformed tool call, we want to inspect what we sent it. `ollama_tool_specs()` returns a literal list — `print(specs)` and there it all is. No registration order, no import-time side effects.

Adding a tool is one entry. That's the framework cost we're willing to pay.

## What we'd revisit when

- **Approval as a modal vs inline cards.** We picked inline cards in the transcript with Approve/Deny buttons. If the user starts missing approval prompts because they scrolled away, switch to a modal that demands attention before the next message can be sent.
- **Parallel tool calls.** Sequential dispatch was deliberate (ADR 0003 §2). The day a tool is naturally parallelizable — three web_search calls in a single turn, say — the loop's inner `for tc in tool_calls` becomes `await asyncio.gather(...)`. The retry logic and approval gating get fiddly; revisit only when it's actually worth it.
- **Frame types as Pydantic models on the server side.** Right now the server constructs frame dicts as literals. If the protocol grows past ten or so types, switch to typed frame classes for help from the type checker.
