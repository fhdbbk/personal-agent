# Frontend

What we learned while building [frontend/](../../frontend/) during [2026-05-02](../sessions/2026-05-02-phase-1-chat-mvp.md) and shaking out networking on [2026-05-03](../sessions/) (LAN access from phone). The CORS / dev-proxy story lives in [cors.md](cors.md); this doc is about the React/TS code itself.

This is written for someone fluent in Python who is meeting JavaScript and React for the first time. We'll lean on Python analogues when they help.

## The shape

```
frontend/
├── index.html              # SPA shell — single <div id="root">
├── src/
│   ├── main.tsx            # 7 lines: createRoot → <App />
│   ├── App.tsx             # the entire chat UI + WS client (~180 lines)
│   ├── App.css             # component styles
│   └── index.css           # html/body resets
├── vite.config.ts          # dev proxy: /chat (ws:true), /health → :8000
├── package.json            # React 19, Vite 8, TypeScript 6
├── tsconfig.{app,node}.json
└── eslint.config.js
```

Two runtime dependencies: `react`, `react-dom`. Everything else is dev tooling. No router, no state library, no UI kit — Phase 1 doesn't need them.

## The JS/TS reflexes that catch a Python person off-guard

### `const` and `let`

```ts
const x = 5
let   y = 5
```

`const` makes the *binding* immutable — you can't reassign `x`. The underlying object can still be mutated (you can push to a `const` array). `let` is the same with reassignment allowed. There's no Python equivalent; treat `const` as the default and only use `let` when you genuinely need to rebind.

### Destructuring on the left of `=`

```ts
const [a, b] = [1, 2]
const {role, content} = msg
```

Square-brackets unpack arrays; curly-braces unpack object fields by name. Same idea as Python's `a, b = (1, 2)` and `**` unpacking, just with different syntax. **The brackets on the left don't build anything — they unpack.**

### Arrow functions

```ts
(ev) => { ...statements...; return x }
(ev) => ev.data            // single expression — implicit return
() => 'hello'
```

Covers both Python's `lambda` and `def` use cases in one syntax. Multi-statement lambdas are normal. Used everywhere as inline callbacks.

### Object/array spread

```ts
{ ...last, content: newContent }   // {**last, 'content': newContent}
[...prev, x]                        // prev + [x]
```

Build a new object/array from an old one with overrides or extras. Foundational for React's immutable-update style.

### Type assertions: `value as T`

```ts
const frame = JSON.parse(ev.data) as ServerFrame
```

`JSON.parse` returns `any`. `as ServerFrame` tells the type checker "trust me, this is the right shape." **Zero runtime check** — same as Python's `typing.cast`. If we ever care about runtime validation, we'd reach for `zod` (JS) or `pydantic` (Python).

### Generic type arguments: `useState<T>(...)`

```ts
useState<ChatMessage[]>([])
```

Calls `useState` and tells the type checker "the `T` in the signature is `ChatMessage[]`." Without this, TS would infer `never[]` from the empty array and reject any later push. Python doesn't really let you spell this; it's mostly inferred from the value.

`T[]` is shorthand for `Array<T>` — same thing.

### `JSON.parse` / `JSON.stringify`

Direct rename of `json.loads` / `json.dumps`. Returns plain objects, arrays, and primitives.

## React's mental model in one paragraph

A function component is a plain function React calls every time it wants to repaint. Local variables don't survive across calls. **Hooks** (functions starting with `use…`) are how a component remembers things between renders. The render reads state; events/effects call setters with a *new* value; React queues, re-renders, repeats. The whole `slice()` / spread / `[...prev, x]` apparatus exists because that loop only works if React can tell "this is a new value." Mutating in place breaks the signal.

Coming from Python, the immutable-update reflex is the one that takes practice. `messages.append(x)` would be the natural move, but here you build a new list.

## `useState` vs `useRef`

Both persist across renders. The difference:

|  | `useState` | `useRef` |
|---|---|---|
| Triggers re-render on change | **yes** | no |
| You read | the snapshot at this render | always `.current` (live) |
| Use when | the UI depends on the value | bookkeeping, DOM handles, mutable plumbing |

In [App.tsx:46-54](../../frontend/src/App.tsx#L46-L54):

- **State** — `messages`, `input`, `busy`, `error`, `conn`. Each is rendered somewhere; flipping it must repaint.
- **Refs** — `conversationIdRef` (a stable id we send with every WS message), `wsRef` (the live socket), `scrollerRef` (handle to the messages `<div>` for imperative scrolling).

Idiom: `useState` returns `[value, setter]`; you destructure and conventionally name the setter `setX`. There's no language rule, just convention everyone follows.

### The setter's two forms

```ts
setMessages(newArray)              // direct value
setMessages(prev => newArray)      // callback — receives the latest queued value
```

The callback form matters when several updates land in the same tick (e.g. a burst of streaming token frames). React batches, so the direct form would read stale `messages` from this render's closure and lose updates. The callback form gets handed the freshest queued value, so updates compose. **Token frames stream fast; the reducer in [App.tsx:75-85](../../frontend/src/App.tsx#L75-L85) must use the callback form or it'd race.**

### Why `useRef` for the conversation id

`useRef(initial)` runs `initial` only on first render and returns the same wrapper object on every render. We use it for the conversation id because it's plumbing the renderer doesn't care about — changing it shouldn't re-render anything. State would be wrong here for the same reason a Python `@cached_property` is wrong if you don't want change notifications.

Tiny caveat: `useRef(newConversationId())` calls `newConversationId()` on *every* render even though only the first result is kept. For a uuid that's free; for expensive init, the lazy form is `useRef<T>(null)` then assign on first read.

## The streaming append reducer

The single most non-obvious piece of code in the file is the `ws.onmessage` handler — [App.tsx:73-92](../../frontend/src/App.tsx#L73-L92):

```ts
ws.onmessage = (ev) => {
  const frame = JSON.parse(ev.data) as ServerFrame
  if (frame.type === 'token') {
    setMessages((prev) => {
      const next = prev.slice()
      const last = next[next.length - 1]
      if (last && last.role === 'assistant') {
        next[next.length - 1] = { ...last, content: last.content + frame.delta }
      } else {
        next.push({ role: 'assistant', content: frame.delta })
      }
      return next
    })
  } else if (frame.type === 'done') {
    setBusy(false)
  } else if (frame.type === 'error') {
    setError(frame.error)
    setBusy(false)
  }
}
```

In Python pseudocode:

```python
def on_message(ev):
    frame = json.loads(ev.data)  # cast to ServerFrame
    if frame['type'] == 'token':
        def updater(prev):
            nxt = prev[:]                                 # shallow copy
            last = nxt[-1] if nxt else None
            if last and last['role'] == 'assistant':
                nxt[-1] = {**last, 'content': last['content'] + frame['delta']}
            else:
                nxt.append({'role': 'assistant', 'content': frame['delta']})
            return nxt
        set_messages(updater)
    elif frame['type'] == 'done':
        set_busy(False)
    elif frame['type'] == 'error':
        set_error(frame['error'])
        set_busy(False)
```

Two ideas worth flagging:

1. **Append in place to the in-flight assistant bubble.** First token after the user sends arrives with no assistant message at the tail, so we push one. Subsequent tokens see an assistant message at `nxt[-1]` and grow its `content`. Net effect: the reply expands as one bubble, not a new bubble per token.

2. **Copy-then-mutate is fine; mutate-prev is not.** We `.slice()` to get a fresh array, then `push` / index-assign freely. React only checks identity at the array level — a new top-level array signals "value changed." Mutating `prev` directly would keep the same reference and React wouldn't re-render. The discipline is: **values you got *from* React are read-only; values you just created are yours to mutate.**

`prev.slice()` ≡ `prev[:]` in Python (shallow copy). `[...prev]` (spread) does the same.

`{ ...last, content: ... }` ≡ Python's `{**last, 'content': ...}` — build a new object, override one field. This is why we don't write `last.content += frame.delta`: that would mutate the message in place and break React's change detection downstream.

## `ServerFrame` — discriminated unions

[App.tsx:11-28](../../frontend/src/App.tsx#L11-L28):

```ts
interface TokenFrame { type: 'token'; delta: string; conversation_id: string }
interface DoneFrame  { type: 'done'; conversation_id: string }
interface ErrorFrame { type: 'error'; error: string; conversation_id?: string }
type ServerFrame = TokenFrame | DoneFrame | ErrorFrame
```

`type` is the **discriminant** — the literal-typed field that lets TS narrow the union. After `if (frame.type === 'token')`, TS knows `frame` is `TokenFrame` and `frame.delta` is valid. Inside `else if (frame.type === 'error')`, `frame.error` is valid. Without a discriminant TS would force optional-chain everything.

Python's nearest equivalent is `typing.Literal['token']` plus a `match`/`isinstance` narrowing.

## `useEffect` — running code after a render

```ts
useEffect(() => {
  const el = scrollerRef.current
  if (el) el.scrollTop = el.scrollHeight
}, [messages])
```

`useEffect(fn, deps)` runs `fn` **after the DOM commits**, whenever any value in `deps` has changed since the last render. Two things to internalise:

- *After* commit, not during render. By the time the effect fires, the new message is laid out, so `scrollHeight` already includes it. Setting `scrollTop = scrollHeight` pins the viewport to the bottom.
- The dependency array decides re-run frequency: `[messages]` runs on each new message; `[]` runs once on mount; *omitting deps* runs after every render (almost always wrong — typing in the textarea would re-scroll).

## The WebSocket lifecycle and the StrictMode trap

[App.tsx:61-106](../../frontend/src/App.tsx#L61-L106) is one effect that opens the socket on mount and closes it on unmount. Two guards in there look paranoid but aren't:

```ts
ws.onopen = () => {
  if (wsRef.current === ws) setConn('open')
}
ws.onclose = () => {
  if (wsRef.current === ws) {
    wsRef.current = null
    setConn('closed')
  }
}
```

**Why the guard.** In dev, React `<StrictMode>` mounts every effect *twice* on purpose, to surface bugs in cleanup. The first mount opens WS₁ and immediately schedules cleanup; the second mount opens WS₂ and overwrites `wsRef.current`. WS₁'s cleanup then fires `close`, and *its* `onclose` handler runs — but it's stale. Without the guard, the stale socket's `onclose` would set `conn` to `'closed'` and null out `wsRef`, killing the live WS₂ from the user's perspective.

The fix is the cheapest possible: each handler closes over its own `ws` and only updates state if `wsRef.current` still points at it. Stale callbacks become no-ops.

`<StrictMode>` only doubles in dev; production renders once. The guard is defensive against dev-time noise, not a runtime feature.

## Connecting to the backend

### `wsUrl` — building the WS URL from the page's location

[App.tsx:39-42](../../frontend/src/App.tsx#L39-L42):

```ts
function wsUrl(path: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}${path}`
}
```

The WebSocket connects back to the **same host:port that served the page**, not a hardcoded `localhost`. That's *why* loading `http://192.168.1.100:5173` from a phone works — JS reads `window.location.host` (= `192.168.1.100:5173`), opens `ws://192.168.1.100:5173/chat/stream`, and Vite's proxy (with `ws: true`) forwards it to FastAPI. If we'd hardcoded `localhost`, the phone would try to open a WS to *itself*.

`http→ws`, `https→wss` is the universal mapping; same scheme rules apply for cookies and security.

### Vite proxy with `ws: true`

[vite.config.ts:11-14](../../frontend/vite.config.ts#L11-L14):

```ts
proxy: {
  '/health': { target: BACKEND, changeOrigin: true },
  '/chat':   { target: BACKEND, changeOrigin: true, ws: true },
},
```

Without `ws: true`, Vite would proxy HTTP to `/chat` but **silently 404 the WebSocket upgrade**. That bit costs an afternoon to debug if you forget it. `changeOrigin: true` rewrites the `Host` header so the backend sees the upstream origin, which matters for some servers' virtual-host routing — for FastAPI on a single port it's harmless either way.

The deeper context (CORS, why the proxy exists, what changes in production) is in [cors.md](cors.md).

### `vite --host` — the difference between `127.0.0.1` and `0.0.0.0`

[package.json:7](../../frontend/package.json#L7):

```json
"dev": "vite --host"
```

The default `vite` binds the dev server to `127.0.0.1` only. The `--host` flag binds to `0.0.0.0` (= all interfaces). That's the difference between "only this machine can reach it" and "anyone on the LAN can." Confirm with `ss -tlnp | grep 5173`: `*:5173` means all interfaces; `127.0.0.1:5173` means localhost only.

The same flag exists on uvicorn (`--host 0.0.0.0`); we hit the same gotcha there.

## LAN access from another device — the full plumbing

This took several wrong turns to get working. The path from phone to component, in order:

1. **App must bind `0.0.0.0`.** `vite --host` for the frontend; `uvicorn --host 0.0.0.0` for the backend. Verify with `ss -tlnp`.
2. **WSL2 networking mode = `mirrored`** in `~/.wslconfig` on the Windows side. Without it, WSL gets its own NAT'd IP and LAN devices can't reach it directly. With mirrored mode, WSL shares the host's network adapter — `ip addr show eth0` reports the same `192.168.x.y` the laptop has on Wi-Fi.
3. **Windows Defender Firewall** must allow inbound on the port, scoped to the *current* network profile (Domain/Private/Public — Wi-Fi often starts as Public). Add via elevated PowerShell:
   ```powershell
   New-NetFirewallRule -DisplayName 'PA Frontend (5173)' -Direction Inbound -Protocol TCP -LocalPort 5173 -Action Allow -Profile Private,Domain
   ```
4. **Hyper-V firewall** for the WSL VM also blocks inbound by default — the Defender rule alone isn't enough in mirrored mode. The `AllowHostPolicyMerge` setting is supposed to merge Defender rules in, but in practice we still had to add explicit Hyper-V rules:
   ```powershell
   New-NetFirewallHyperVRule -Name "WSL-PA-5173" -DisplayName "PA Frontend (5173)" -Direction Inbound `
     -VMCreatorId "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" -Protocol TCP -LocalPorts 5173 -Action Allow
   ```
   The VM creator GUID `{40E0AC32-...}` is the standard one for WSL; verify with `Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore`.
5. **The LAN IP is DHCP, so it shifts.** Pin it with a router-side reservation if you don't want to keep grabbing the new IP.

Diagnostic order when it breaks:
- `ss -tlnp | grep <port>` — is the server even listening, and on what?
- `curl http://<lan-ip>:<port>/...` from WSL — proves the binding is right.
- `Test-NetConnection -ComputerName <lan-ip> -Port <port>` from Windows PowerShell — fails *here* means firewall (Defender or Hyper-V).
- Phone test only after the host can reach itself on the LAN IP.

## Pitfalls worth knowing

- **Mutating state directly is the most common React bug for Python devs.** `messages.push(x)` followed by `setMessages(messages)` gives React the same array reference; identity check passes; no re-render. Always build a new array/object.
- **`useState([])` with no generic infers `never[]`** in TypeScript. Either annotate (`useState<ChatMessage[]>([])`) or pass an initial value the compiler can read a type from.
- **`useEffect` with no deps array runs after every render.** Different from `[]` (run once). The empty array is what most "on mount" effects want; omitting the array is almost always a mistake.
- **`<StrictMode>` doubles dev mounts on purpose.** If something works in production but not dev, suspect missing cleanup before suspecting React.
- **Forgetting `ws: true` on the Vite proxy** turns WS upgrades into 404s without any helpful error. The HTTP fallback works fine, which is what makes it confusing.
- **`window.location.host` includes the port; `window.location.hostname` doesn't.** Use `host` when building URLs, `hostname` when matching against bare names.
