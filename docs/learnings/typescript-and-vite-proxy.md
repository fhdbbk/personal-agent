# TypeScript and Vite Proxy Learnings

This document captures Q&A learnings from a code review session of the frontend (`App.tsx` and `vite.config.ts`), specifically bridging mental models from Python and C++ to modern TypeScript and React.

## The Journey of a WebSocket Message & Vite Proxy

When a user submits a message, it travels to the FastAPI backend through a development-time proxy configured in Vite.

1. **The Origin**: The browser creates a WebSocket connection using `wsUrl('/chat/stream')`. Since the UI runs on a local dev server (e.g., `localhost:5173`), this resolves to `ws://localhost:5173/chat/stream`.
2. **The Proxy Trap (`vite.config.ts`)**: 
   ```typescript
   proxy: {
     '/chat': { target: 'http://127.0.0.1:8000', changeOrigin: true, ws: true }
   }
   ```
   Vite intercepts any request starting with the `/chat` prefix. It acts as a reverse proxy, silently forwarding this traffic to the backend on port `8000`. This completely sidesteps CORS issues because the browser thinks it's only talking to its own origin (`5173`).
3. **Why `http` instead of `ws`?** A WebSocket connection always begins its life as an HTTP `GET` request with `Upgrade: websocket` headers. The Vite proxy catches this initial HTTP request, honors the `ws: true` flag by upgrading the connection, and then pipes the raw TCP stream back and forth.

## TypeScript: `type` vs `interface`

In modern TypeScript, both are used to define shapes, but they map to different concepts in Python/C++.

- **`type` (Type Alias)**: Analogous to C++'s `using`/`typedef` or Python's `TypeAlias`. It creates a name for *any* type configuration. It is uniquely capable of defining **Unions** (`type Role = 'user' | 'assistant'`) and Intersections.
- **`interface`**: Analogous to C++ Pure Virtual Structs or Python Abstract Base Classes. It strictly defines the contract of an object/dictionary. Interfaces are open to inheritance (`extends`) and "declaration merging" (defining the same interface twice merges their properties). 

**Rule of thumb**: Use `interface` for JSON object shapes (like `TokenFrame`) and component props. Use `type` for unions, primitives, and tuples.

## Discriminated Unions vs Simple Unions

In `App.tsx`, `TranscriptItem` is defined as a union of two object shapes: a message or a tool. 

```typescript
type TranscriptItem = 
  | { kind: 'message'; id: string; role: Role; content: string }
  | { kind: 'tool'; id: string; call_id: string; ... }
```

By adding a literal string field (`kind`) to both shapes, we created a **Discriminated Union**. 
- A simple union (`{ role... } | { call_id... }`) forces you to write fragile type guards like `if ('role' in item)` to figure out which shape you have.
- A discriminated union provides an exact tag. When you write `if (item.kind === 'message')`, TypeScript perfectly narrows the type, granting access to `.content` while throwing a compiler error if you try to access `.call_id`. It also provides exhaustiveness checking in `switch` statements.

## The Tool Call Lifecycle and Optional Properties (`?`)

```typescript
result?: { ok: boolean; preview: string }
```
The `?` syntax marks the `result` property as optional (`undefined | { ok, preview }`). This models the async lifecycle of a tool call:
1. **Initiation**: The backend announces a tool call is starting. The UI renders the tool card, but `result` is left `undefined`.
2. **Completion**: The backend finishes the tool and sends a `tool_result` frame. The UI updates the item, populating the `result` object.

*(Note: The `preview` field contains truncated output rather than the full `content`. The backend feeds the full output to the LLM but truncates it for the frontend to prevent massive payloads from locking up the browser UI).*

## The ES6 Module System: `export default`

```typescript
export default function App() { ... }
```
- **`export`**: Makes the function visible to other files (similar to declaring it in a C++ header file).
- **`default`**: Marks it as the primary export of the file. 

When another file imports a `default` export, it can omit curly braces and name the import whatever it wants (`import ChatApp from './App'`). If the word `default` is omitted, it becomes a "Named Export", which strictly requires curly braces and the exact function name (`import { App } from './App'`). In React, UI component files conventionally use default exports.
