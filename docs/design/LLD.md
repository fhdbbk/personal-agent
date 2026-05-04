# Low-Level Design

> Module-by-module reference for the personal assistant. Each section has: responsibilities → public API → key data structures → diagrams → `file:line` anchors. For the bird's-eye view, see [HLD](HLD.md).

## Table of contents

1. [Process and module map](#1-process-and-module-map)
2. [Data structures shared across modules](#2-data-structures-shared-across-modules)
3. [API layer — HTTP and WebSocket](#3-api-layer--http-and-websocket)
4. [Frame protocol (WebSocket)](#4-frame-protocol-websocket)
5. [Agent loop](#5-agent-loop)
6. [Tool registry](#6-tool-registry)
7. [Tools and sandbox](#7-tools-and-sandbox)
8. [Memory — short-term ring buffer](#8-memory--short-term-ring-buffer)
9. [Prompt assembly](#9-prompt-assembly)
10. [Configuration](#10-configuration)
11. [Logging](#11-logging)
12. [Frontend](#12-frontend)
13. [Phase 3 — long-term memory (planned)](#13-phase-3-long-term-memory-planned)
14. [Phase 4 — voice I/O (planned)](#14-phase-4-voice-io-planned)
15. [Phase 5 — calendar tools (planned)](#15-phase-5-calendar-tools-planned)
16. [Phase 6 — hot paths (planned)](#16-phase-6-hot-paths-planned)
17. [Phase 7 — mobile (planned)](#17-phase-7-mobile-planned)

---

## 1. Process and module map

```mermaid
flowchart TB
    subgraph proc ["uvicorn process"]
        main["main.py<br/>FastAPI app, CORS, /health"]
        chat["api/chat.py<br/>routers + WS handler"]
        loop["agent/loop.py<br/>run_turn(), _dispatch_tool()"]
        prompt["agent/prompt.py<br/>system_prompt()"]
        registry["tools/registry.py<br/>Tool, TOOLS, ollama_tool_specs()"]
        readf["tools/read_file.py"]
        writef["tools/write_file.py"]
        sandbox["tools/_sandbox.py<br/>safe_path()"]
        buffer["memory/buffer.py<br/>ConversationBuffer"]
        cfg["config.py<br/>Settings, get_settings()"]
        logc["logging_config.py<br/>configure_logging()"]

        main --> chat
        main --> cfg
        main --> logc
        chat --> loop
        chat --> prompt
        chat --> buffer
        chat --> cfg
        loop --> registry
        loop --> cfg
        registry --> readf
        registry --> writef
        readf --> sandbox
        writef --> sandbox
        sandbox --> cfg
        prompt -.->|reads| soul[("SOUL.md")]
        readf --> sb[("sandbox/")]
        writef --> sb
        logc --> logfiles[("logs/pa.log*")]
    end

    proc <-->|WS + HTTP| browser(["Browser"])
    proc -->|httpx async| ollama(["Ollama daemon"])
```

The whole backend is a single uvicorn process. All singletons (`get_settings()`, `_client()`, `buffer`) live for the process lifetime.

---

## 2. Data structures shared across modules

These few small types flow between layers. Keeping them here avoids duplicating the definitions in every section that touches them.

| Type | Location | Shape | Notes |
|---|---|---|---|
| `Message` | [memory/buffer.py:8](../../backend/app/memory/buffer.py#L8) | `dataclass(frozen=True)` with `role: Literal["user","assistant"]`, `content: str` | Frozen so the buffer can't be mutated under callers; hashable by side-effect |
| `Role` | [memory/buffer.py:5](../../backend/app/memory/buffer.py#L5) | `Literal["user", "assistant"]` | Tool messages use the raw `"tool"` string in the loop, not this alias |
| `Tool` | [tools/registry.py:21](../../backend/app/tools/registry.py#L21) | `dataclass(frozen=True)`: `name`, `fn`, `schema`, `requires_approval` | `fn: Callable[..., Awaitable[str]]` — async returning a string |
| `ChatRequest` | [api/chat.py:18](../../backend/app/api/chat.py#L18) | Pydantic: `conversation_id: str (min_length=1)`, `message: str (min_length=1)` | Reused by both HTTP and WS endpoints |
| `ChatResponse` | [api/chat.py:23](../../backend/app/api/chat.py#L23) | Pydantic: `conversation_id`, `reply` | HTTP only |
| `ResetRequest` / `ResetResponse` | [api/chat.py:28](../../backend/app/api/chat.py#L28), [:32](../../backend/app/api/chat.py#L32) | Pydantic | Used by `POST /chat/reset` |
| `Settings` | [config.py:10](../../backend/app/config.py#L10) | `pydantic-settings` BaseSettings, env prefix `PA_`, reads `.env` | Singleton via `@lru_cache get_settings()` |
| `OnEvent` | [agent/loop.py:24](../../backend/app/agent/loop.py#L24) | `Callable[[dict[str, Any]], Awaitable[None]]` | Push-only callback for emitting frames |
| `RequestApproval` | [agent/loop.py:25](../../backend/app/agent/loop.py#L25) | `Callable[[str, str, dict[str, Any]], Awaitable[bool]]` | Pull callback: `(call_id, name, args) → approved` |
| `AgentError` | [agent/loop.py:28](../../backend/app/agent/loop.py#L28) | Exception | Loop couldn't produce an answer (max steps, repeated tool errors) |
| `SandboxError` | [tools/_sandbox.py:13](../../backend/app/tools/_sandbox.py#L13) | `ValueError` subclass | Path argument escaped the sandbox |
| `ToolError` | [tools/registry.py:16](../../backend/app/tools/registry.py#L16) | Exception | Defined but currently unused; tools return error strings, not exceptions |

```mermaid
classDiagram
    class Message {
        <<frozen dataclass>>
        +role: Role
        +content: str
    }
    class Tool {
        <<frozen dataclass>>
        +name: str
        +fn: async (...) -> str
        +schema: dict
        +requires_approval: bool
    }
    class ConversationBuffer {
        -_maxlen: int
        -_convos: dict[str, deque[Message]]
        +append(cid, message)
        +history(cid) list[Message]
        +clear(cid)
    }
    class Settings {
        <<BaseSettings>>
        +ollama_host: str
        +ollama_model: str
        +ollama_think: bool
        +ollama_device: Device
        +request_timeout_s: float
        +agent_sandbox: str
        +agent_max_steps: int
        +agent_max_retries_per_tool: int
        +agent_auto_approve: bool
        +log_dir: str
        +log_level: str
    }
    ConversationBuffer "1" *-- "*" Message : stores
```

---

## 3. API layer — HTTP and WebSocket

**File**: [backend/app/api/chat.py](../../backend/app/api/chat.py) · **Logger**: `pa.chat`

### Responsibilities

- Accept HTTP requests for non-streaming chat and conversation reset.
- Hold the long-lived WebSocket and drive one agent turn per inbound message.
- Construct the message list passed to the loop (system prompt + history + new turn).
- Persist completed turns to the conversation buffer.
- Manage the lazy singleton Ollama [`AsyncClient`](https://github.com/ollama/ollama-python).

### Endpoints

| Method | Path | Request | Response | Use |
|---|---|---|---|---|
| `POST` | `/chat` | `ChatRequest` | `ChatResponse` | Non-streaming, no tools. Fallback / smoke. [chat.py:73](../../backend/app/api/chat.py#L73) |
| `POST` | `/chat/reset` | `ResetRequest` | `ResetResponse` | Drop a conversation from the buffer. [chat.py:102](../../backend/app/api/chat.py#L102) |
| `WS` | `/chat/stream` | (frames) | (frames) | The main entry point. [chat.py:109](../../backend/app/api/chat.py#L109) |
| `GET` | `/health` | — | `{status, ollama_host, ollama_model}` | [main.py:34](../../backend/app/main.py#L34) |

### `_build_messages()` and `_persist_turn()`

Two tiny helpers used by both the HTTP and WS handlers. [`_build_messages`](../../backend/app/api/chat.py#L46) prepends `{"role":"system", "content": system_prompt()}` (re-read fresh — see [§9](#9-prompt-assembly)), spreads the history out as `{role, content}` dicts, and appends the new user turn. [`_persist_turn`](../../backend/app/api/chat.py#L54) appends the user message *and* the assistant reply to the buffer in one go.

### `_device_options()`

Translates `PA_OLLAMA_DEVICE` into an Ollama `options` dict ([chat.py:59](../../backend/app/api/chat.py#L59)):

| `PA_OLLAMA_DEVICE` | Returns | Effect |
|---|---|---|
| `auto` (default) | `None` | Ollama decides |
| `cpu` | `{"num_gpu": 0}` | Force CPU-only |
| `gpu` | `{"num_gpu": 999}` | Full offload (Ollama clamps to actual layer count) |

Currently used **only by `POST /chat`**. The agent loop streaming call doesn't pass `options` — see ADR [0004](../decisions/0004-streaming-with-tools.md) and [§5 below](#5-agent-loop).

### WebSocket connection state

```mermaid
stateDiagram-v2
    [*] --> connecting
    connecting --> open: ws.accept()
    open --> in_turn: receive {cid, message}
    in_turn --> awaiting_approval: tool requires approval<br/>(send tool_approval)
    awaiting_approval --> in_turn: receive approval_response<br/>(matching call_id)
    in_turn --> open: send done
    in_turn --> open: AgentError → send error
    open --> closed: WebSocketDisconnect
    in_turn --> closed: WebSocketDisconnect
    awaiting_approval --> closed: WebSocketDisconnect
    closed --> [*]
```

While in `awaiting_approval`, any frame whose `type ≠ "approval_response"` or whose `call_id` doesn't match is logged and discarded ([chat.py:161-170](../../backend/app/api/chat.py#L161-L170)). The UI disables the composer while a turn is in flight, so this discard branch is defensive.

### Callback wiring (`on_event`, `request_approval`)

The WS handler defines two closures per turn ([chat.py:146-170](../../backend/app/api/chat.py#L146-L170)) and passes them into the agent loop. The loop knows nothing about WebSockets — it just calls the callbacks. This makes `run_turn()` trivially testable with plain async fakes; see [docs/learnings/agent-loop-architecture.md](../learnings/agent-loop-architecture.md).

```python
async def on_event(frame: dict) -> None:
    await ws.send_json({**frame, "conversation_id": req.conversation_id})

async def request_approval(call_id: str, name: str, args: dict) -> bool:
    await ws.send_json({"type": "tool_approval", ...})
    while True:
        frame = await ws.receive_json()
        if frame.get("type") == "approval_response" and frame.get("call_id") == call_id:
            return bool(frame.get("approved"))
        log.warning("ws unexpected frame during approval: %s", frame.get("type"))
```

---

## 4. Frame protocol (WebSocket)

The complete contract for `WS /chat/stream`. Defined in code at [chat.py:113-127](../../backend/app/api/chat.py#L113-L127); duplicated here in tabular form.

### Server → Client

| `type` | Required fields | Sent when | Example |
|---|---|---|---|
| `token` | `delta: str` | Each non-empty content chunk from the stream | `{"type":"token","delta":"Hello","conversation_id":"c-…"}` |
| `tool_call` | `call_id, name, args` | Loop is about to dispatch a tool | `{"type":"tool_call","call_id":"call_a1b2c3","name":"read_file","args":{"path":"notes.txt"},"conversation_id":"c-…"}` |
| `tool_approval` | `call_id, name, args` | Tool needs user approval (`requires_approval=True` and not auto-approve) | `{"type":"tool_approval","call_id":"call_…","name":"write_file","args":{…},"conversation_id":"c-…"}` |
| `tool_result` | `call_id, ok: bool, preview: str` | Tool finished (success, error, or denial); `preview` is ≤500 chars of the full result | `{"type":"tool_result","call_id":"call_…","ok":true,"preview":"hello\n","conversation_id":"c-…"}` |
| `done` | — | Turn completed successfully | `{"type":"done","conversation_id":"c-…"}` |
| `error` | `error: str` | Loop raised `AgentError` or an unhandled exception | `{"type":"error","error":"agent exceeded MAX_STEPS=8…","conversation_id":"c-…"}` |

Every server frame carries `conversation_id` — added by `on_event` in the spread `{**frame, "conversation_id": …}` ([chat.py:147](../../backend/app/api/chat.py#L147)).

### Client → Server

| `type` (or shape) | Fields | Sent when |
|---|---|---|
| (turn start, no `type` field) | `conversation_id, message` | User submits the composer |
| `approval_response` | `call_id, approved: bool` | User clicks Approve or Deny on a tool card |

### Versioning strategy

Per ADR [0002](../decisions/0002-chat-transport.md) §"Versioning": the discriminated-union shape lets us add new `type` values without breaking old clients. A client that doesn't recognise a frame type can ignore it (today's UI [App.tsx:126](../../frontend/src/App.tsx#L126) silently drops unknown types). New required fields on existing types are a breaking change and must coordinate UI + backend.

---

## 5. Agent loop

**File**: [backend/app/agent/loop.py](../../backend/app/agent/loop.py) · **ADRs**: [0003](../decisions/0003-agent-loop.md), [0004](../decisions/0004-streaming-with-tools.md) · **Logger**: `pa.agent`

### Responsibilities

- Drive a single user turn to a final answer.
- Stream content tokens out as they arrive (every iteration).
- Dispatch `tool_calls` sequentially, gating with approval where required.
- Cap iteration count and retry-per-tool failures so a stuck loop fails loudly.

### `run_turn()` — the algorithm

Pseudocode (real implementation at [loop.py:74-197](../../backend/app/agent/loop.py#L74-L197)):

```python
msgs = list(base_messages)
tool_specs = ollama_tool_specs()
consecutive_errors, last_failed_tool = 0, None

for step in range(MAX_STEPS):
    stream = client.chat(model, msgs, tools=tool_specs, stream=True, think=…)

    content_chunks, final_tool_calls = [], []
    async for chunk in stream:
        if chunk.message.content:
            content_chunks.append(chunk.message.content)
            await on_event({"type": "token", "delta": chunk.message.content})
        if chunk.message.tool_calls:
            final_tool_calls = list(chunk.message.tool_calls)   # last one wins

    content = "".join(content_chunks)
    if not final_tool_calls:
        return content                                          # done

    msgs.append({"role": "assistant", "content": content,
                 "tool_calls": [tc.model_dump() for tc in final_tool_calls]})

    for tc in final_tool_calls:
        name, args = tc.function.name, dict(tc.function.arguments or {})
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        await on_event({"type": "tool_call", "call_id": call_id, "name": name, "args": args})
        ok, result = await _dispatch_tool(name, args, call_id=call_id, request_approval=…)
        await on_event({"type": "tool_result", "call_id": call_id, "ok": ok,
                        "preview": _preview(result)})
        if ok:
            consecutive_errors, last_failed_tool = 0, None
        else:
            consecutive_errors = consecutive_errors + 1 if last_failed_tool == name else 1
            last_failed_tool = name
            if consecutive_errors > MAX_RETRIES_PER_TOOL:
                raise AgentError(...)
        msgs.append({"role": "tool", "content": result})        # full text, not preview

raise AgentError(f"exceeded MAX_STEPS={MAX_STEPS}")
```

A few details worth knowing before changing this:

- **Streaming is unconditional.** Even with `tools=[…]` available, we stream and forward tokens. `tool_calls` arrive complete in a late chunk (probed against `qwen3.5:4b` — see [docs/learnings/streaming-with-tools.md](../learnings/streaming-with-tools.md)). If a future Ollama version splits them across chunks, the "last one wins" assignment is the spot to revisit ([loop.py:122](../../backend/app/agent/loop.py#L122)).
- **The assistant turn is built by hand.** When tools are involved we don't have a single `Message` object to `model_dump()` — we construct the dict explicitly with `content + tool_calls` ([loop.py:139-145](../../backend/app/agent/loop.py#L139-L145)). Easy to forget.
- **Model sees full results, UI sees previews.** The `preview` field on `tool_result` is truncated by `_preview(text, n=500)` ([loop.py:33](../../backend/app/agent/loop.py#L33)); the full text goes back to the model ([loop.py:193](../../backend/app/agent/loop.py#L193)).
- **Retries are per-tool.** A different tool's failure resets the counter ([loop.py:181-185](../../backend/app/agent/loop.py#L181-L185)).
- **Approval denial is not an error.** `_dispatch_tool` returns `(ok=True, "User denied this action.")` so the model can adapt without burning a retry slot ([loop.py:55-58](../../backend/app/agent/loop.py#L55-L58)).

### `_dispatch_tool()` decision table

| Condition | Returned `(ok, body)` | Counter effect |
|---|---|---|
| `name not in TOOLS` | `(False, "unknown tool: 'x'. available: [...]")` | Increments (or starts a new streak) |
| `requires_approval` and not auto-approve, user denies | `(True, "User denied this action.")` | Resets |
| Tool raises `TypeError` (bad/missing kwargs) | `(False, "argument error: <msg>")` | Increments |
| Tool raises any other exception | `(False, "<ExcClass>: <msg>")` | Increments |
| Tool returns non-string | `(True, str(result))` | Resets |
| Tool returns string | `(True, result)` | Resets |

### Sequence diagrams

**(a) Plain streaming chat — no tools fired**

```mermaid
sequenceDiagram
    autonumber
    participant UI as Browser UI
    participant API as WS handler
    participant Loop as run_turn()
    participant Ollama
    participant Buf as buffer

    UI->>API: {cid, message}
    API->>Loop: run_turn(base_msgs, on_event, request_approval)
    Loop->>Ollama: chat(stream=True, tools=[...])
    loop streaming
        Ollama-->>Loop: chunk(content)
        Loop->>API: on_event({type:"token", delta})
        API-->>UI: token frame
    end
    Ollama-->>Loop: chunk(done, no tool_calls)
    Loop-->>API: return content
    API->>Buf: append(user, assistant)
    API-->>UI: {type:"done", cid}
```

**(b) Tool turn with approval — the canonical Phase 2 flow**

```mermaid
sequenceDiagram
    autonumber
    participant UI as Browser UI
    participant API as WS handler
    participant Loop as run_turn()
    participant Disp as _dispatch_tool()
    participant Tool as write_file()
    participant Ollama

    UI->>API: {cid, "save a note"}
    API->>Loop: run_turn(...)
    Loop->>Ollama: chat(stream=True, tools=[...])
    Ollama-->>Loop: stream<br/>(some content)<br/>+ tool_calls=[write_file{...}]
    Loop->>API: on_event(token...) (during stream)
    Loop->>API: on_event({tool_call, call_id, name, args})
    API-->>UI: tool_call frame
    Loop->>Disp: _dispatch_tool(write_file, args)
    Disp->>API: request_approval(call_id, name, args)
    API-->>UI: tool_approval frame
    Note over UI: user clicks Approve
    UI->>API: {approval_response, call_id, approved:true}
    API-->>Disp: True
    Disp->>Tool: await write_file(**args)
    Tool-->>Disp: "wrote 12 chars to note.md"
    Disp-->>Loop: (ok=True, "wrote ...")
    Loop->>API: on_event({tool_result, call_id, ok, preview})
    API-->>UI: tool_result frame
    Note over Loop: append {"role":"tool", content:full}<br/>loop again
    Loop->>Ollama: chat(stream=True, tools=[...])
    loop streaming
        Ollama-->>Loop: chunk(content)
        Loop->>API: on_event(token)
        API-->>UI: token frame
    end
    Ollama-->>Loop: chunk(done, no tool_calls)
    Loop-->>API: return content
    API-->>UI: {type:"done", cid}
```

**(c) Tool error + retry, then fail-out**

```mermaid
sequenceDiagram
    autonumber
    participant Loop as run_turn()
    participant Disp as _dispatch_tool()
    participant Tool as read_file()
    participant Ollama

    Note over Loop: consecutive_errors=0
    Loop->>Disp: read_file(path="missing.txt")
    Disp->>Tool: await read_file(...)
    Tool--xDisp: FileNotFoundError
    Disp-->>Loop: (False, "FileNotFoundError: no such file: missing.txt")
    Note over Loop: counter → 1<br/>(MAX_RETRIES=2, allowed)
    Loop->>Ollama: chat(...)<br/>(model sees error)
    Ollama-->>Loop: tool_calls=[read_file(path="still-missing.txt")]
    Loop->>Disp: read_file(...)
    Tool--xDisp: FileNotFoundError
    Disp-->>Loop: (False, "...")
    Note over Loop: counter → 2 (still allowed)
    Loop->>Ollama: chat(...)
    Ollama-->>Loop: tool_calls=[read_file(path="nope.txt")]
    Loop->>Disp: read_file(...)
    Disp-->>Loop: (False, "...")
    Note over Loop: counter → 3 > MAX_RETRIES_PER_TOOL=2
    Loop--xLoop: raise AgentError("tool 'read_file' failed 3 times in a row...")
```

**(d) Approval denied — not counted as an error**

```mermaid
sequenceDiagram
    participant UI as Browser UI
    participant Loop as run_turn()
    participant Disp as _dispatch_tool()

    Loop->>Disp: write_file(...)
    Disp->>UI: request_approval (via WS)
    UI-->>Disp: approved=false
    Disp-->>Loop: (ok=True, "User denied this action.")
    Note over Loop: counter unchanged.<br/>append {"role":"tool", "User denied..."}<br/>loop again — model sees the denial<br/>and decides what to do
```

---

## 6. Tool registry

**File**: [backend/app/tools/registry.py](../../backend/app/tools/registry.py) · **ADR**: [0003 §3](../decisions/0003-agent-loop.md)

A dict, not a framework. The whole module is 47 lines.

```mermaid
classDiagram
    class Tool {
        <<frozen dataclass>>
        +name: str
        +fn: async (...) -> str
        +schema: dict
        +requires_approval: bool
    }

    class TOOLS {
        <<dict[str, Tool]>>
    }

    class read_file_tool {
        name = "read_file"
        fn = read_file
        schema = READ_FILE_SCHEMA
        requires_approval = false
    }
    class write_file_tool {
        name = "write_file"
        fn = write_file
        schema = WRITE_FILE_SCHEMA
        requires_approval = true
    }

    TOOLS --> read_file_tool
    TOOLS --> write_file_tool
    read_file_tool --|> Tool
    write_file_tool --|> Tool
```

`ollama_tool_specs()` ([registry.py:45](../../backend/app/tools/registry.py#L45)) is the single export the loop needs — a list of the `schema` dicts ready to pass to `client.chat(tools=...)`.

### Adding a tool

Three steps, no decorators:

1. Create `backend/app/tools/<your_tool>.py` with an async `fn(**kwargs) -> str` and a module-level `SCHEMA: dict` (use [read_file.py](../../backend/app/tools/read_file.py) as the template — Ollama's expected JSON-schema shape is `{"type":"function","function":{"name","description","parameters"}}`).
2. Import both in [registry.py](../../backend/app/tools/registry.py) and add an entry to `TOOLS` with the right `requires_approval`.
3. (Optional) Add a smoke test in `scripts/`.

That's it. The loop picks it up via `ollama_tool_specs()` on the next turn.

---

## 7. Tools and sandbox

**Files**: [tools/read_file.py](../../backend/app/tools/read_file.py), [tools/write_file.py](../../backend/app/tools/write_file.py), [tools/_sandbox.py](../../backend/app/tools/_sandbox.py)

### `read_file` ([read_file.py](../../backend/app/tools/read_file.py))

| Aspect | Value |
|---|---|
| Schema params | `{"path": str}`, required |
| Approval | No |
| Returns | UTF-8 text of the file (errors-replace) |
| Truncation | At `MAX_BYTES = 64_000` (~16k tokens at 4 chars/token), with a footer noting the original size |
| Errors | `SandboxError` (path escapes), `FileNotFoundError`, `IsADirectoryError` — all surface as `(ok=False, …)` to the loop |
| Async | Reads via `asyncio.to_thread(p.read_bytes)` so the event loop isn't blocked |

### `write_file` ([write_file.py](../../backend/app/tools/write_file.py))

| Aspect | Value |
|---|---|
| Schema params | `{"path": str, "content": str}`, both required |
| Approval | **Yes** (`requires_approval=True`) |
| Returns | `"wrote N chars to <path>"` |
| Behaviour | Creates parent directories; **overwrites** existing files |
| Errors | `SandboxError`; `OSError` from the underlying write |
| Async | Writes via `asyncio.to_thread(p.write_text, content, "utf-8")` |

### `_sandbox.safe_path()` ([_sandbox.py:23](../../backend/app/tools/_sandbox.py#L23))

The single boundary check shared by every file tool.

```mermaid
flowchart TB
    in(["user_path: str"]) --> empty{empty or<br/>whitespace-padded?}
    empty -- yes --> err1[/SandboxError/]
    empty -- no --> resolve["target = (root / user_path).resolve()"]
    resolve --> contains{target.is_relative_to(root)?}
    contains -- no --> err2[/SandboxError: escapes sandbox/]
    contains -- yes --> ok(["return target: Path"])
```

Why `Path.resolve()` first: it collapses `..` and follows symlinks, so the containment check operates on the *real* final path. `Path.is_relative_to()` is the canonical containment predicate on Python 3.9+.

**What's caught**:
- `../../etc/passwd` (relative escape) → resolves outside root → rejected
- `/etc/passwd` (absolute) → resolves to itself → not under sandbox root → rejected
- `link-to-outside` (symlink to outside) → resolves to target → rejected
- Whitespace-padded `" foo.txt "` → rejected as ill-formed

**What's not caught** (out of scope per ADR 0003 §5):
- A model that spawns a subprocess from `python_exec` (Phase 2 second session — relies on `subprocess + rlimits`, not `safe_path`)
- TOCTOU races between resolve-time and use-time
- A determined adversary controlling the model

---

## 8. Memory — short-term ring buffer

**File**: [backend/app/memory/buffer.py](../../backend/app/memory/buffer.py) · **ADR**: [0001](../decisions/0001-tech-stack.md)

40 lines. Per-conversation `deque(maxlen=32)`, keyed in a dict. No persistence — Phase 3 introduces SQLite for long-term memory ([§13 below](#13-phase-3-long-term-memory-planned)).

### API

```python
buffer.append(cid, Message(role="user", content="..."))
buffer.append(cid, Message(role="assistant", content="..."))
history: list[Message] = buffer.history(cid)
buffer.clear(cid)
```

### Retention

Per-conversation, last **32 messages** total. With user/assistant alternation that's ~16 round-trips before old context starts dropping. With tools the *user-visible* turn count is preserved (we only persist user + final assistant), but the history seen by Ollama within a single tool-using turn can include many `tool` messages — those are intra-turn and never hit the buffer.

### Lifetime

Module-level singleton `buffer = ConversationBuffer()` ([buffer.py:40](../../backend/app/memory/buffer.py#L40)). Lives as long as the uvicorn process. **Restarts wipe it.** This is intentional for Phase 1/2; Phase 3 adds the SQLite backing.

---

## 9. Prompt assembly

**File**: [backend/app/agent/prompt.py](../../backend/app/agent/prompt.py)

```python
SOUL_PATH = Path(__file__).resolve().parents[3] / "SOUL.md"

def system_prompt() -> str:
    return SOUL_PATH.read_text(encoding="utf-8").strip()
```

Two design choices worth knowing:

- **It's a function, not a constant**, so [SOUL.md](../../SOUL.md) is re-read on every turn. Persona edits are hot.
- The path math is `parents[3]`: `prompt.py` → `agent/` → `app/` → `backend/` → repo root. If the file moves, this number changes.

The cost is one small file read per turn (negligible). The benefit is rapid iteration on the assistant's voice without restarting the server.

---

## 10. Configuration

**File**: [backend/app/config.py](../../backend/app/config.py)

`pydantic-settings`-based. Values come from process env vars (prefixed `PA_`) or a `.env` at the repo root. Singleton via `@lru_cache get_settings()`. Read-once at startup.

| Setting (Python) | Env var | Default | Used by |
|---|---|---|---|
| `ollama_host` | `PA_OLLAMA_HOST` | `http://localhost:11434` | `chat._client()`, `/health` |
| `ollama_model` | `PA_OLLAMA_MODEL` | `qwen3.5:4b` | `chat`, `loop` |
| `ollama_think` | `PA_OLLAMA_THINK` | `false` | `chat`, `loop` (Qwen3's reasoning mode) |
| `ollama_device` | `PA_OLLAMA_DEVICE` | `auto` | `chat._device_options()` only — not the loop today |
| `request_timeout_s` | `PA_REQUEST_TIMEOUT_S` | `60.0` | Per-chunk idle timeout for streaming Ollama calls |
| `agent_sandbox` | `PA_AGENT_SANDBOX` | `sandbox` | `_sandbox.sandbox_root()` |
| `agent_max_steps` | `PA_AGENT_MAX_STEPS` | `8` | `loop.run_turn()` |
| `agent_max_retries_per_tool` | `PA_AGENT_MAX_RETRIES_PER_TOOL` | `2` | `loop.run_turn()` |
| `agent_auto_approve` | `PA_AGENT_AUTO_APPROVE` | `false` | `loop._dispatch_tool()` (skip approval) |
| `log_dir` | `PA_LOG_DIR` | `logs` | `logging_config` |
| `log_level` | `PA_LOG_LEVEL` | `INFO` | `logging_config` |

Type aliases: `Device = Literal["auto", "cpu", "gpu"]` ([config.py:7](../../backend/app/config.py#L7)).

---

## 11. Logging

**File**: [backend/app/logging_config.py](../../backend/app/logging_config.py) · **Called from**: [main.py:11](../../backend/app/main.py#L11)

`configure_logging()` runs at import time (before the FastAPI app object is constructed) so import-time messages are captured. It:

1. Resolves and creates `${PA_LOG_DIR}` if missing.
2. Builds a single `Formatter`: `"%(asctime)s %(levelname)-7s %(name)s: %(message)s"`.
3. Attaches a `TimedRotatingFileHandler` (`when="midnight"`, `backupCount=7`, local time) and a `StreamHandler` (stderr) to the **root logger**.
4. Sets the root level from `PA_LOG_LEVEL`.
5. **Reroutes** uvicorn's loggers (`uvicorn`, `uvicorn.access`, `uvicorn.error`) by clearing their handlers and setting `propagate = True`, so HTTP traffic lands in the same file.

### Logger names

| Name | Used in | What it logs |
|---|---|---|
| `pa.main` | [main.py](../../backend/app/main.py) | Startup banner |
| `pa.chat` | [api/chat.py](../../backend/app/api/chat.py) | Endpoint entry/exit, latency, ws connect/disconnect, bad-request warnings |
| `pa.agent` | [agent/loop.py](../../backend/app/agent/loop.py) | Per-step iteration, tool failures, final reply length |
| `uvicorn*` | (rerouted) | HTTP access + errors |

Daily rotation produces `pa.log`, `pa.log.YYYY-MM-DD`, …; the oldest is dropped after 7 backups.

---

## 12. Frontend

**File**: [frontend/src/App.tsx](../../frontend/src/App.tsx) · **Build**: [vite.config.ts](../../frontend/vite.config.ts) · **Lessons**: [docs/learnings/frontend.md](../learnings/frontend.md)

A single-file React app — chat UI, WS client, frame reducer, and the `ToolCard` subcomponent.

### State shape

| Variable | Type | Purpose |
|---|---|---|
| `transcript` | `TranscriptItem[]` | Ordered list of messages and tool cards |
| `input` | `string` | Composer textarea |
| `busy` | `boolean` | A turn is in flight (disables composer + New) |
| `error` | `string \| null` | Last error message banner |
| `conn` | `'connecting' \| 'open' \| 'closed'` | WS connection state, shown as a status dot |
| `conversationId` | `string` | Client-generated id (`"c-" + UUID`); stable until "New" is pressed |
| `wsRef` | `RefObject<WebSocket \| null>` | Live socket handle; ref because mutating it shouldn't re-render |
| `scrollerRef` | `RefObject<HTMLDivElement \| null>` | Messages container, used to auto-scroll on transcript change |

### `TranscriptItem` (discriminated union)

```ts
type TranscriptItem =
  | { kind: 'message'; id, role: 'user'|'assistant'; content }
  | { kind: 'tool'; id, call_id, name, args, awaitingApproval; result?: { ok, preview } }
```

Tool cards are interleaved into the transcript so they appear inline at the right moment.

### Frame reducer

[`handleFrame()`](../../frontend/src/App.tsx#L126) is a switch on `frame.type`. All `setTranscript` calls use the **callback form** (`prev => ...`) so fast-arriving token frames compose correctly.

| Frame `type` | Reducer action | Notes |
|---|---|---|
| `token` | If last item is an assistant message → append `delta`; else push a new assistant message | Streaming append uses `last.content + frame.delta` |
| `tool_call` | Upsert a tool card by `call_id` | Card created on first sight of this `call_id` |
| `tool_approval` | Upsert + set `awaitingApproval: true` | Renders Approve / Deny buttons on the card |
| `tool_result` | Find by `call_id`, set `result` and clear `awaitingApproval` | The card ends in `ok` or `error` styling |
| `done` | Set `busy: false` | Composer re-enabled |
| `error` | Set `error`, `busy: false` | Banner shown above the composer |

[`upsertTool()`](../../frontend/src/App.tsx#L178) handles the "tool_approval might arrive before tool_call" race by creating-or-patching by `call_id`.

### WebSocket lifecycle

Single connection opened in a `useEffect` with `[]` deps ([App.tsx:99-124](../../frontend/src/App.tsx#L99-L124)). Cleanup closes the socket. `onopen`/`onerror`/`onclose` handlers all check `wsRef.current === ws` to **guard against React StrictMode double-mounts** in dev, which would otherwise have stale handlers from the first mount fire on the second mount's socket.

### Component tree

```mermaid
flowchart TB
    main["main.tsx<br/>createRoot"] --> app["<App />"]
    app --> hdr["header (status dot, cid, New button)"]
    app --> msgs["messages container"]
    msgs --> msg["msg (user / assistant)"]
    msgs --> card["<ToolCard /><br/>(args, status, result, approval buttons)"]
    app --> banner["error banner"]
    app --> composer["composer (textarea + Send)"]
```

### "New chat" flow

`newChat()` ([App.tsx:247](../../frontend/src/App.tsx#L247)) rotates `conversationId` (so the next message starts a fresh history on the server) and best-effort `POST /chat/reset` to evict the old conversation from the buffer. The fetch is fire-and-forget; the rotated id alone is enough.

### Dev proxy and LAN access

[vite.config.ts](../../frontend/vite.config.ts) proxies `/chat` (with `ws: true`) and `/health` to `:8000` so the browser sees same-origin requests in dev. `wsUrl()` builds the WS URL from `window.location` so connecting from a phone on the LAN (`http://<laptop-ip>:5173`) "just works." WSL2-specific networking notes (mirrored mode, firewall rules) are in [docs/learnings/frontend.md](../learnings/frontend.md).

---

## 13. Phase 3 — long-term memory (planned)

> All planned-phase sections below are **design intent**, not implemented behaviour. They will get LLD entries equal in detail once the code exists.

### Goal

Persist facts across restarts and across conversations. On each turn, retrieve the top-k most-relevant facts and prepend them to the system message.

### Components to add

```mermaid
classDiagram
    class ConversationBuffer {
        <<existing>>
        short-term, in-process
    }
    class LongTermStore {
        <<new — SQLite>>
        +add_fact(text, embedding, metadata)
        +search(query_embedding, k) list[Fact]
    }
    class Embedder {
        <<new — sentence-transformers>>
        +embed(text) ndarray
    }
    class FactExtractor {
        <<new>>
        +extract(turn) list[Fact]
    }
    class Retriever {
        +recall(user_message, k) list[Fact]
    }
    Retriever --> Embedder
    Retriever --> LongTermStore
    FactExtractor --> Embedder
    FactExtractor --> LongTermStore
```

### Storage sketch

A single SQLite file under `${PA_DATA_DIR}` (new env var). One table for facts:

```sql
CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    embedding BLOB NOT NULL,        -- packed float32
    created_at TEXT NOT NULL,
    source TEXT,                    -- e.g. "cid:c-abc:turn:7"
    weight REAL DEFAULT 1.0
);
```

Embeddings are 384-dim (`all-MiniLM-L6-v2`), stored as packed `float32` BLOB. Top-k is brute-force cosine similarity in Python — adequate for thousands of facts, swap to a vector index later if needed.

### Retrieval flow (where it slots in)

```mermaid
sequenceDiagram
    participant API as WS handler
    participant Retr as Retriever
    participant Embed as Embedder
    participant Store as LongTermStore
    participant Loop as run_turn()

    API->>Retr: recall(user_message, k=5)
    Retr->>Embed: embed(user_message)
    Embed-->>Retr: vector[384]
    Retr->>Store: search(vector, k=5)
    Store-->>Retr: list[Fact]
    Retr-->>API: list[Fact]
    Note over API: build msgs = [<br/>  system: SOUL + "Things you remember:" + facts,<br/>  ...history...,<br/>  user_message<br/>]
    API->>Loop: run_turn(base_msgs, ...)
```

### Fact extraction

Either an in-loop step (the agent itself emits facts via a `remember(text)` tool) or a post-turn pass (a small classifier scans the turn for declarative statements). Lean toward the tool approach — it's simpler and lets the user see what's being remembered (could even be approval-gated).

### Open questions for Phase 3

- Where does retrieval happen relative to streaming? (Probably: synchronously before the first stream call — adds a few hundred ms but happens once per turn.)
- How does "forget X" work — soft-delete with `weight = 0`, hard-delete, or a `forgotten` flag?
- Do we re-embed on model swap, or pin to one embedding model permanently?

---

## 14. Phase 4 — voice I/O (planned)

### Topology

A **separate worker process** for the audio models (faster-whisper, Piper) so model loading and inference don't block the FastAPI event loop. The worker talks to FastAPI over a Unix socket or an in-process queue.

```mermaid
flowchart LR
    ui["UI: mic capture<br/>(MediaRecorder)"]
    api["api/audio.py<br/>(new)"]
    worker["audio worker<br/>(faster-whisper + Piper)"]
    loop["agent/loop.py"]

    ui -- "audio frames<br/>(WS, type:audio_in)" --> api
    api -- "PCM" --> worker
    worker -- "transcript" --> api
    api --> loop
    loop -- "reply text" --> api
    api -- "synth request" --> worker
    worker -- "audio" --> api
    api -- "audio frames<br/>(WS, type:audio_out)" --> ui
```

### New frame types

| `type` | Direction | Fields |
|---|---|---|
| `audio_in` | C → S | `chunk: base64`, `sample_rate`, `format` |
| `audio_in_end` | C → S | (signals end of utterance) |
| `transcript` | S → C | `text`, `final: bool` (interim transcripts during streaming ASR) |
| `audio_out` | S → C | `chunk: base64`, `sample_rate`, `format` |

Existing `token`, `tool_*`, `done`, `error` frames are unchanged.

### Open questions for Phase 4

- Push-to-talk vs VAD-driven? Start with PTT for simplicity.
- Streaming ASR (interim transcripts) vs batch (one final transcript)? faster-whisper supports both.
- TTS at sentence boundaries vs at the end of the turn? Sentence-level is much better UX but needs a sentence boundary detector that doesn't break on partial tokens.

---

## 15. Phase 5 — calendar tools (planned)

No structural change. Adds tool entries to `TOOLS`:

| Tool | Approval | Notes |
|---|---|---|
| `calendar_list_events(start, end)` | No | Read-only |
| `calendar_create_event(title, start, end, …)` | **Yes** | Default approval-gated |
| `calendar_modify_event(id, …)` | **Yes** | |
| `calendar_delete_event(id)` | **Yes** | |

A small `backend/app/integrations/calendar.py` holds the auth handshake and a credentials cache. Likely Google Calendar via OAuth, or CalDAV for self-hosted setups.

---

## 16. Phase 6 — hot paths (planned)

Profile after Phase 4 lands. Likely candidates for FFI replacement:

| Hot spot | Why hot | FFI shape |
|---|---|---|
| Embedder tokenisation / normalisation | Called on every turn (retrieval) and every fact extraction | `cffi` shim around a small C tokeniser |
| Cosine similarity over many facts | Scales linearly with fact count; pure-Python is fine to ~10k facts then degrades | `pybind11` wrapper around a SIMD-aware impl |
| `safe_path` boundary check | Only if it shows up in profiling — probably won't | n/a |

The pattern: pure-Python implementation stays as the fallback; the FFI version is loaded if available. Tests pin the contract on both.

---

## 17. Phase 7 — mobile (planned)

Open question; revisit when the rest is solid. The three options and what each implies architecturally:

| Option | Architecture impact |
|---|---|
| **React Native** | Reuse most of the React component layer; backend stays on the laptop, phone is a thin client over the LAN. Lowest backend churn. |
| **Native Android** | Best UX, biggest rewrite (Kotlin/Compose). Backend stays separate; phone talks the existing WS protocol. |
| **PWA** | Fastest path; works in any phone browser today. Limits voice (no mic permission backgrounded), no proper background tasks. |

The decision affects whether the backend remains "always-on at home, phone talks to it" or whether parts of the stack move on-device. Out of scope until everything else ships.

---

## Appendix — file index

| Path | Lines | Role |
|---|---|---|
| [backend/app/main.py](../../backend/app/main.py) | ~42 | FastAPI app, CORS, `/health`, router mount |
| [backend/app/api/chat.py](../../backend/app/api/chat.py) | ~215 | HTTP + WS endpoints, frame protocol |
| [backend/app/agent/loop.py](../../backend/app/agent/loop.py) | ~198 | `run_turn()`, `_dispatch_tool()` |
| [backend/app/agent/prompt.py](../../backend/app/agent/prompt.py) | ~10 | `system_prompt()` (SOUL.md hot-loader) |
| [backend/app/tools/registry.py](../../backend/app/tools/registry.py) | ~48 | `Tool`, `TOOLS`, `ollama_tool_specs()` |
| [backend/app/tools/read_file.py](../../backend/app/tools/read_file.py) | ~43 | read tool + schema |
| [backend/app/tools/write_file.py](../../backend/app/tools/write_file.py) | ~38 | write tool + schema |
| [backend/app/tools/_sandbox.py](../../backend/app/tools/_sandbox.py) | ~37 | `safe_path()`, `sandbox_root()`, `SandboxError` |
| [backend/app/memory/buffer.py](../../backend/app/memory/buffer.py) | ~41 | `Message`, `ConversationBuffer`, `buffer` singleton |
| [backend/app/config.py](../../backend/app/config.py) | ~35 | `Settings`, `get_settings()` |
| [backend/app/logging_config.py](../../backend/app/logging_config.py) | ~57 | `configure_logging()` |
| [frontend/src/App.tsx](../../frontend/src/App.tsx) | ~375 | UI, WS client, frame reducer, `ToolCard` |
| [frontend/vite.config.ts](../../frontend/vite.config.ts) | — | Dev proxy for `/chat` (ws) and `/health` |
| [scripts/smoke_*.py](../../scripts/) | — | End-to-end test drivers (no UI required) |
