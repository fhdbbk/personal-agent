# 2026-05-01 — Phase 0: Foundations

## Goal

Lay the groundwork for the personal assistant: settle the tech stack, scaffold the backend, set up docs discipline, and prove that we can talk to a local LLM end-to-end.

## What we did

1. **Planned the architecture.** Wrote a phased roadmap (0–7) targeting a local-first agentic assistant on a 16 GB CPU laptop. Confirmed four key decisions with the user:
   - LLM runtime: **Ollama**
   - Phase 1 scope: **text-only chat MVP** (voice and tools come later)
   - Agent loop: **build from scratch**, no framework
   - Docs: scaffold all three doc folders in Phase 0 and write the first ADR
2. **Verified the environment.** Ollama 0.6.5 is installed; `llama3.2:latest` is already pulled. uv 0.6.14 is installed. Project pins Python 3.12.
3. **Scaffolded the backend.**
   - [backend/app/config.py](../../backend/app/config.py) — `pydantic-settings` config with `PA_*` env vars (host, model, timeout)
   - [backend/app/main.py](../../backend/app/main.py) — minimal FastAPI app with `/health`
   - Added deps via `uv add`: `fastapi`, `uvicorn[standard]`, `ollama`, `pydantic`, `pydantic-settings`
4. **Wrote the smoke test.** [scripts/smoke_ollama.py](../../scripts/smoke_ollama.py) lists models, fails loudly if the configured one isn't pulled, and pings the model with a tiny prompt.
5. **Set up docs.** Created [docs/learnings/](../learnings/), [docs/sessions/](.), and [docs/decisions/](../decisions/) each with a README explaining its purpose. Wrote [0001-tech-stack.md](../decisions/0001-tech-stack.md).
6. **Smoke-tested end to end.**
   - `GET /health` → `200 {"status":"ok","ollama_host":"http://localhost:11434","ollama_model":"llama3.2:latest"}`
   - `scripts/smoke_ollama.py` → "Pong" in 4.08 s on CPU.

## Decisions made

- [ADR 0001: Initial Tech Stack](../decisions/0001-tech-stack.md)

## Snags + fixes

- **Smoke script couldn't import `backend.app.config`.** uv doesn't auto-install our local code as a package, so `from backend.app.config import get_settings` failed with `ModuleNotFoundError`. Quick fix: prepend the project root to `sys.path` at the top of the script. **Better fix later**: declare the project as a proper package in `pyproject.toml` (e.g. add a hatchling build config and `[tool.hatch.build.targets.wheel].packages = ["backend"]`) so `uv sync` installs it. Defer until we have more scripts that need this.
- **Model choice.** The user's preference is `qwen3.5:4b`. We didn't pull it this session because (a) it's a 2–3 GB download and (b) `llama3.2:latest` is already on the machine and is good enough for a smoke test. Pull `qwen3.5:4b` (or whichever sibling model proves to be the one in Ollama's registry) before Phase 1's first real chat turn.

## Open threads / next session

- Pull the chosen Qwen model and update `PA_OLLAMA_MODEL` (or the default in [config.py](../../backend/app/config.py)).
- Phase 1 kickoff: implement `/chat` (HTTP + WebSocket streaming), short-term conversation buffer, and a minimal Vite + React + TS chat UI.
- Decide where to put a `.env.example` and how to wire the React dev proxy to the FastAPI port.
- Wire [SOUL.md](../../SOUL.md) into the system prompt of every LLM call. Added late in this session as a peer to CLAUDE.md — CLAUDE.md is for us, SOUL.md is for the assistant. The prompt builder should load it at request time so personality edits are hot.
- Fahad to review SOUL.md and rewrite the voice/values sections in his own words; the current draft is a generic starter.
