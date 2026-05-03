# 0002 — Chat transport: long-lived WebSocket with JSON frame envelopes

**Status:** Accepted
**Date:** 2026-05-02
**Phase:** 1

## Context

Phase 1 ships a text chat MVP. We need a wire protocol for browser ↔ FastAPI that:

- streams tokens as the model produces them (CPU inference is slow; `wait, then drop a wall of text` feels broken),
- carries enough metadata that Phase 2 can layer tool-call traffic on the same channel without a redesign,
- stays trivial to debug from the command line.

Three axes of choice:

1. **Streaming primitive.** WebSocket vs Server-Sent Events vs HTTP chunked.
2. **Connection lifecycle.** One socket per turn vs one socket per session.
3. **Frame shape.** Raw text deltas vs typed JSON envelopes.

## Decision

1. **WebSocket** at `ws://.../chat/stream`. SSE is one-way and would need a separate POST for the user's input; the symmetry of WS keeps client code small and gives us a clean upgrade path when Phase 2 starts pushing tool-call requests *from* the assistant *to* the client (e.g. "approve this `python_exec`?").
2. **Long-lived socket, multi-turn.** The client opens once at mount and reuses for every turn. Avoids reconnect/handshake latency between turns and matches the natural shape of a chat session.
3. **Typed JSON envelopes.** Server → client:
   ```json
   {"type": "token", "delta": "...", "conversation_id": "..."}
   {"type": "done",  "conversation_id": "..."}
   {"type": "error", "error": "...", "conversation_id": "..."}
   ```
   Client → server: `{conversation_id, message}`. Both sides agree the `type` discriminant is mandatory; new types (`tool_call`, `tool_result`, `thought`) get added in Phase 2 without breaking older clients.

## Why not the alternatives

- **SSE.** Simpler on paper, but the asymmetry forces a separate `POST /chat` to send the user's turn, splitting state between two requests. We'd also have to invent our own framing on top of `data:` lines.
- **One socket per turn.** Adds ~50–200 ms of WS handshake to every reply, and the server can't push unsolicited frames (e.g. tool approval prompts) once we get to Phase 2.
- **Raw text deltas.** Cheaper to write today but forces a protocol change the moment we add anything besides assistant tokens. The JSON envelope cost is two `json.dumps` per token, which is negligible next to the model.

## Consequences

- Phase 2 can introduce `{"type": "tool_call", ...}` frames on the same socket without a transport rewrite.
- Conversation ids are client-generated UUIDs sent on every turn — server is stateless about identity and just keys the in-memory ring buffer by whatever string the client sends. We'll revisit when Phase 3's persistent memory needs server-owned conversation ids.
- Errors do not close the socket; the client sees `{"type": "error"}` and can keep sending. Connection-level failures (FastAPI restart, network drop) reopen on next mount.
