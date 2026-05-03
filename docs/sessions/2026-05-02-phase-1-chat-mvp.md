# 2026-05-02 — Phase 1: Text chat MVP

## Goal

Ship the first end-to-end chat: browser → FastAPI → Ollama → streamed back to the browser. Wire SOUL.md as the system prompt. Keep a short-term conversation buffer keyed by `conversation_id`. No tools, no persistence — those are Phases 2 and 3.

## What we did

1. **Pulled `qwen3.5:4b`.** Local Ollama was 0.6.5, which 412'd on the new manifest. Upgraded Ollama (`curl …/install.sh | sh`) to 0.22.1, then `ollama pull qwen3.5:4b` (3.4 GB). Switched the default in [backend/app/config.py](../../backend/app/config.py) from `llama3.2:latest` to `qwen3.5:4b`.
2. **Built the backend.** New packages under [backend/app/](../../backend/app/):
   - [memory/buffer.py](../../backend/app/memory/buffer.py) — `ConversationBuffer` is a `dict[str, deque[Message]]` with a maxlen cap. Single module-level `buffer` singleton; we'll grow it later.
   - [agent/prompt.py](../../backend/app/agent/prompt.py) — reads `SOUL.md` from the repo root on every call. No caching, so personality edits take effect on the next request.
   - [api/chat.py](../../backend/app/api/chat.py) — `POST /chat` (non-streaming) and `WS /chat/stream` (token-streamed). Both build the same message list: `[system, …history, user]`. Persistence to the buffer happens *after* a successful turn so a failed call doesn't pollute history.
   - [main.py](../../backend/app/main.py) — mounts the chat router and adds permissive CORS for `localhost:5173` (Vite dev).
3. **Wrote ADR 0002.** [docs/decisions/0002-chat-transport.md](../decisions/0002-chat-transport.md) captures the WS-vs-SSE, long-lived-vs-per-turn, and JSON-envelope decisions.
4. **Frontend scaffold.** Installed `nvm` (no Node was on the box), Node 24 LTS, then `npm create vite@latest frontend -- --template react-ts`. Replaced the demo `App.tsx`/CSS with a small chat UI that opens one WS at mount and streams assistant messages in place. [vite.config.ts](../../frontend/vite.config.ts) proxies `/chat` (with `ws: true`) and `/health` to `127.0.0.1:8000` so the SPA can use same-origin paths. Stripped the scaffold's hero/logo assets and demo CSS.
5. **Smoke tests, all passing on warm model.**
   - `POST /chat` → 1-shot reply (`{"reply":"pong"}`).
   - Two-turn buffer test: turn 1 said "favorite color is teal", turn 2 asked "what is my favorite color?" → "Teal". Buffer wiring confirmed.
   - [scripts/smoke_chat_ws.py](../../scripts/smoke_chat_ws.py) — direct WS to FastAPI, prints tokens and reports TTFT.
   - [scripts/smoke_chat_ws_proxy.py](../../scripts/smoke_chat_ws_proxy.py) — same payload but through the Vite proxy, confirms the WS upgrade is forwarded.
6. **Docs.** Wrote [README.md](../../README.md) (run instructions + layout), [.env.example](../../.env.example), updated [CLAUDE.md](../../CLAUDE.md)'s decisions table and repo layout.

## Decisions made

- [ADR 0002: Chat transport](../decisions/0002-chat-transport.md) — long-lived WebSocket, multi-turn per connection, typed JSON frames.

## Snags + fixes

- **Ollama 0.6.5 couldn't pull `qwen3.5:4b`** (manifest requires a newer Ollama). Fix: upgrade Ollama via the official install script. Needed `sudo`, so Fahad ran it himself.
- **No Node on the machine.** Installed nvm under `~/.nvm` and Node 24 LTS. Per-shell PATH activation is needed (`. ~/.nvm/nvm.sh`) — already appended to `~/.bashrc` by the installer.
- **Cold-start latency.** First WS turn ttft was 14 s, warm was 9 s on a 4B model on CPU. Acceptable for now; Phase 6 will profile and replace hot paths.
- **Tested wiring, not the rendered UI.** I can't drive a browser from this environment. `npm run dev` serves on :5173, `/health` and `/chat/stream` proxy through fine, but Fahad needs to open the page and confirm token streaming actually paints in the UI as expected.

## Open threads / next session

- **Browser smoke test.** Open <http://localhost:5173>, confirm tokens stream into the assistant bubble in real time and that buffer recall works across turns. Catch any CSS/UX papercuts.
- **`request_timeout_s` is plumbed through config but not actually applied** to the Ollama calls. Pass it into the `AsyncClient` (or via `asyncio.wait_for`) before something hangs.
- **Buffer never clears.** No endpoint to start a new conversation or drop history. Add `POST /chat/reset` (or just trust the client to rotate `conversation_id`) — decide before Phase 3.
- **Phase 2 prep.** Next phase is the agent loop with tools. ADR 0002's typed frames already leave room for `tool_call` / `tool_result`. Sketch the loop on paper before writing code.
- **SOUL.md is still the generic starter.** Fahad to rewrite voice/values in his own words — leftover from Phase 0.
