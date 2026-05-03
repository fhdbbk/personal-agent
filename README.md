# Personal Assistant

Local-first agentic personal assistant. Runs on a laptop CPU, talks to a local Ollama model, keeps short-term and (eventually) long-term memory. See [Project-Idea.md](Project-Idea.md) for the high-level pitch and [CLAUDE.md](CLAUDE.md) for the active state.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python deps and runner
- [Ollama](https://ollama.com/) ≥ 0.7 — and `ollama pull qwen3.5:4b`
- Node ≥ 20 (LTS) — for the frontend

## Run it

Three terminals:

```bash
# 1. Backend (FastAPI on :8000)
uv sync
uv run uvicorn backend.app.main:app --reload

# 2. Frontend (Vite on :5173, dev-proxies /chat and /health to :8000)
cd frontend
npm install
npm run dev

# 3. Smoke tests (optional)
curl http://127.0.0.1:8000/health
uv run python scripts/smoke_ollama.py        # model warm-up + ping
uv run python scripts/smoke_chat_ws.py        # streaming WS direct
uv run python scripts/smoke_chat_ws_proxy.py  # streaming via Vite proxy
```

Then open <http://localhost:5173>.

## Configuration

All runtime config lives in [backend/app/config.py](backend/app/config.py) and is overridable via `PA_*` env vars or a `.env` at the repo root. Copy [.env.example](.env.example) to start.

## Layout

```
backend/app/
  api/chat.py        # POST /chat + WS /chat/stream
  agent/prompt.py    # loads SOUL.md as the system prompt
  memory/buffer.py   # in-process conversation ring buffer
  config.py          # PA_* settings
  main.py            # FastAPI entry, /health
frontend/            # Vite + React + TS chat UI
scripts/             # smoke tests
docs/
  decisions/         # ADRs
  sessions/          # one log per working session
  learnings/         # topical notes
SOUL.md              # assistant personality (hot-loaded each turn)
CLAUDE.md            # state for future Claude Code sessions
```
