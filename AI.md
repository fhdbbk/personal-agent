# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Local-first agentic personal assistant. Runs entirely on a laptop CPU (16 GB RAM target) using only open-source models. Accepts text or voice input, replies with text or speech. Maintains short-term and long-term memory. The agent loop calls tools (file I/O, web search, code execution, calendar). Eventually targets mobile.

This is a **learning project**. Where reinventing has educational value (agent loop, tool dispatch, memory) we build from scratch; where it doesn't (LLM inference, ASR/STT, TTS) we use proven libraries.

Source of truth for the high-level design: [Project-Idea.md](Project-Idea.md). Phased roadmap and architecture: see ADRs under [docs/decisions/](docs/decisions/).

## Picking up where we left off

**At the start of every session, read the most recent file in [docs/sessions/](docs/sessions/) before doing anything else.** Its "Open threads / next session" section is the canonical handoff — it tells you what the previous session finished, what's blocked, and what to start next. Don't infer next steps from CLAUDE.md alone; the session log is more current.

**At the end of every session, write a new session log entry** following the conventions in [docs/sessions/README.md](docs/sessions/README.md). Update CLAUDE.md's decisions table when an ADR is added.

## Major design decisions

| Concern | Choice | ADR |
|---|---|---|
| LLM runtime | Ollama (wraps llama.cpp) | [0001](docs/decisions/0001-tech-stack.md) |
| Starting model | `qwen3.5:4b` (default); `llama3.2:latest` is the fallback | [0001](docs/decisions/0001-tech-stack.md) |
| ASR/STT | faster-whisper (CTranslate2) | [0001](docs/decisions/0001-tech-stack.md) |
| TTS | Piper | [0001](docs/decisions/0001-tech-stack.md) |
| Backend | FastAPI + uvicorn | [0001](docs/decisions/0001-tech-stack.md) |
| Frontend | React + Vite + TypeScript | [0001](docs/decisions/0001-tech-stack.md) |
| Short-term memory | In-process ring buffer | [0001](docs/decisions/0001-tech-stack.md) |
| Long-term memory | SQLite + sentence-transformers (`all-MiniLM-L6-v2`) | [0001](docs/decisions/0001-tech-stack.md) |
| Agent loop | Hand-built JSON tool-call protocol — **no framework** (no smolagents, LangChain, LangGraph) | [0001](docs/decisions/0001-tech-stack.md) |
| Chat transport | Long-lived WebSocket, multi-turn, typed JSON frames | [0002](docs/decisions/0002-chat-transport.md) |
| Agent loop shape | Native Ollama tool-calling, sequential ReAct, sandboxed tools | [0003](docs/decisions/0003-agent-loop.md) |
| Streaming + tools | Stream every iteration; tool_calls finalize on the last chunk | [0004](docs/decisions/0004-streaming-with-tools.md) |
| `web_search` backend | `ddgs` library (browser-fingerprinted DDG) — raw HTML scrape was bot-blocked | [0005](docs/decisions/0005-search-backend-ddgs.md) |
| `fetch_url` tool | `primp` (reused from ddgs) + `trafilatura` extractor; approval-gated; public hosts only | [0006](docs/decisions/0006-fetch-url-tool.md) |
| Package manager | uv | [0001](docs/decisions/0001-tech-stack.md) |
| Python | 3.12 | [0001](docs/decisions/0001-tech-stack.md) |

When you make a non-trivial design choice, write a new ADR under [docs/decisions/](docs/decisions/) and add a row above. Never edit an old ADR in place — write a new one and mark the old one Superseded.

## Phased roadmap

Each phase ends with a working, demoable system.

- **Phase 0** — foundations (this is done): backend skeleton, `/health`, Ollama smoke test, docs scaffold.
- **Phase 1** — text-only chat MVP: `/chat` HTTP + WebSocket streaming, conversation buffer, minimal React UI.
- **Phase 2** — agent loop + tools: ReAct-style loop, `read_file`/`write_file`, `web_search`, sandboxed `python_exec`.
- **Phase 3** — long-term memory: SQLite + embeddings, auto-extracted facts, top-k retrieval into context.
- **Phase 4** — voice I/O: mic capture, faster-whisper ASR/STT, Piper TTS, audio streaming.
- **Phase 5** — calendar + richer tools.
- **Phase 6** — profile and replace hot paths with C/C++ via `ctypes` / `cffi` / `pybind11`.
- **Phase 7** — mobile (React Native vs Android vs PWA — decide later).

## SOUL.md — assistant personality

`SOUL.md` (repo root, peer to this file) defines the assistant's *personality* — voice, values, conversational style, what it cares about, what it refuses. It is the persona layer, separate from technical/system prompt scaffolding.

- **CLAUDE.md** is for *us* (future Claude Code instances) — project state, decisions, conventions.
- **SOUL.md** is for *the assistant we're building* — it gets injected into the LLM's system prompt on every turn.

The agent's prompt builder (Phase 1+) reads `SOUL.md` at request time, so personality edits are hot — no code change needed. Keep it terse; small models follow short, vivid system prompts better than long lists of rules.

## Documentation discipline

The user has asked us to keep the project documented as we build. Four folders, each with its own README:

- [docs/sessions/](docs/sessions/) — one file per working session, `YYYY-MM-DD-short-slug.md`. Records what we did, what we decided, and what's left.
- [docs/decisions/](docs/decisions/) — ADRs for significant design choices. Numbered, immutable.
- [docs/design/](docs/design/) — living HLD + LLD with Mermaid diagrams. The synthesised architectural view; cross-references ADRs for the *why*. Refresh after sessions that add or remove a component.
- [docs/learnings/](docs/learnings/) — topic-based notes on what we learned (especially the dead ends).

**Every session should end with a session log entry.** Update CLAUDE.md's decisions table whenever a new ADR is written. Update the relevant section of [docs/design/](docs/design/) whenever a session changes a component's shape (not for in-component edits).

## Repo layout

```
personal_assistant/
├── backend/
│   └── app/
│       ├── api/chat.py         # POST /chat, POST /chat/reset, WS /chat/stream
│       ├── agent/
│       │   ├── prompt.py       # loads SOUL.md as the system prompt
│       │   └── loop.py         # Phase 2 ReAct loop (ADR 0003)
│       ├── tools/
│       │   ├── registry.py     # Tool dataclass + TOOLS dict + ollama specs
│       │   ├── _sandbox.py     # safe_path / sandbox_root for file tools
│       │   ├── list_files.py   # list_files tool — sandbox dir listing
│       │   ├── read_file.py    # read_file tool + JSON schema
│       │   ├── write_file.py   # write_file tool + JSON schema (approval-gated)
│       │   ├── web_search.py   # web_search tool — DDG via ddgs library (ADR 0005)
│       │   └── fetch_url.py    # fetch_url tool — primp + trafilatura, approval-gated (ADR 0006)
│       ├── memory/buffer.py    # in-process conversation ring buffer
│       ├── main.py             # FastAPI entry, /health, router mount, CORS
│       └── config.py           # pydantic-settings, PA_* env vars
│   └── tests/                  # pytest suite (unit tests, no live Ollama)
│       └── test_agent_loop.py  # run_turn behaviour: stats, tools, approval, retries, max-steps
├── frontend/                   # Vite + React + TS chat UI
│   ├── src/App.tsx             # chat + tool transcript + approval buttons
│   └── vite.config.ts          # dev proxy: /chat (ws) and /health → :8000
├── sandbox/                    # gitignored; agent's read/write scope
├── scripts/
│   ├── smoke_ollama.py         # verify Ollama reachable + model can complete
│   ├── smoke_chat_ws.py        # direct WS streaming smoke test
│   ├── smoke_chat_ws_proxy.py  # WS streaming via Vite dev proxy
│   ├── smoke_agent.py          # drive the agent loop through tool calls + approval
│   ├── smoke_web_search.py     # direct ddgs probe (no agent, no LLM)
│   ├── smoke_agent_web_search.py # end-to-end: agent uses web_search
│   ├── smoke_fetch_url.py      # direct fetch_url probe (no agent, no LLM)
│   └── smoke_agent_fetch_url.py # end-to-end: agent chains web_search + fetch_url
├── docs/
│   ├── decisions/              # ADRs (immutable)
│   ├── design/                 # HLD + LLD (living, with Mermaid)
│   ├── learnings/
│   └── sessions/
├── main.py                     # CLI entry placeholder
├── pyproject.toml              # uv-managed deps
├── .env.example
├── Project-Idea.md
├── SOUL.md                     # assistant personality (injected into system prompt)
└── CLAUDE.md
```

Future phases will add `backend/app/audio/` for voice I/O, plus the sandboxed `python_exec` tool.

## Commands

```bash
# Install / sync deps
uv sync

# Run the FastAPI server
uv run uvicorn backend.app.main:app --reload

# Run the frontend dev server (proxies to FastAPI)
cd frontend && npm install && npm run dev

# Hit the health endpoint
curl http://127.0.0.1:8000/health

# Smoke-test Ollama against the configured model
uv run python scripts/smoke_ollama.py

# Smoke-test the chat WebSocket (direct + via Vite proxy)
uv run python scripts/smoke_chat_ws.py
uv run python scripts/smoke_chat_ws_proxy.py

# Smoke-test the agent loop (read_file + write_file with approval)
uv run python scripts/smoke_agent.py

# Smoke-test list_files directly (no agent, no LLM)
uv run python scripts/smoke_list_files.py

# Smoke-test web_search directly (no agent, no LLM) and through the agent
uv run python scripts/smoke_web_search.py
uv run python scripts/smoke_agent_web_search.py

# Smoke-test fetch_url directly and through the agent (search + fetch chain)
uv run python scripts/smoke_fetch_url.py
uv run python scripts/smoke_agent_fetch_url.py

# Run the unit tests (agent loop, no live Ollama needed)
uv run pytest

# Add a dependency (use --group dev for test-only deps)
uv add <package>
```

## Configuration

All runtime config goes through [backend/app/config.py](backend/app/config.py) (pydantic-settings, `PA_*` env vars). Currently:

- `PA_OLLAMA_HOST` (default `http://localhost:11434`)
- `PA_OLLAMA_MODEL` (default `qwen3.5:4b`)
- `PA_OLLAMA_THINK` (default `false` — Qwen3 thinks-before-answering when on; off keeps replies snappy)
- `PA_OLLAMA_DEVICE` (default `auto`; `cpu` forces `num_gpu=0`, `gpu` forces full offload)
- `PA_OLLAMA_NUM_CTX` (default `32768` — Ollama itself defaults to 4096, which is too small once tool results enter the conversation; 32k is qwen2.5/3's native training window. Bump to 65536 if you have the VRAM and accept YaRN-extension quality risk past 32k.)
- `PA_REQUEST_TIMEOUT_S` (default `60`)
- `PA_AGENT_SANDBOX` (default `sandbox` — root for `read_file` / `write_file`)
- `PA_AGENT_MAX_STEPS` (default `8` — abort the loop after this many model turns)
- `PA_AGENT_MAX_RETRIES_PER_TOOL` (default `2` — consecutive errors before failing the turn)
- `PA_AGENT_AUTO_APPROVE` (default `false` — set `true` for headless/dev runs)
- `PA_LOG_DIR` (default `logs` — rotates daily, keeps 7 backups)
- `PA_LOG_LEVEL` (default `INFO`)

A `.env` in the repo root is auto-loaded.

## Conventions

- Don't introduce a framework when ~200 lines of clear code will do — that defeats the learning goal.
- Prefer editing the smoke script / health route / config over adding new abstractions.
- When something works, write down *why* (in a session log or learning doc) before moving on.
- The smoke script imports `backend.app.config` via `sys.path` injection because we haven't packaged the project yet. If multiple scripts start needing this, switch to a real `pyproject.toml` build config instead of duplicating the shim.
