# 0001 — Initial Tech Stack

- **Status**: Accepted
- **Date**: 2026-05-01

## Context

Greenfield project. We need to pick the foundational stack before we write any real code. Constraints from [Project-Idea.md](../../Project-Idea.md):

- Local-first: must run on a laptop CPU with 16 GB RAM.
- Open-source models only.
- Text and voice I/O.
- Agent loop with tools, plus short- and long-term memory.
- The user is new to React — frontend is a learning goal.
- Eventually mobile (React Native or Android), and C/C++ for hot paths.

This is also a **learning project**: where reinventing has educational value (agent loop, tool dispatch, memory), we build from scratch; where it doesn't (LLM inference, ASR/STT, TTS), we use proven libraries.

## Decision

| Concern | Choice |
|---|---|
| LLM runtime | **Ollama** (wraps llama.cpp) |
| Starting model | **Qwen3.5:4b**, with `llama3.2:latest` (already pulled) as the immediate smoke-test fallback |
| ASR/STT | **faster-whisper** (CTranslate2), `base.en` or `small` |
| TTS | **Piper** |
| Backend | **FastAPI + uvicorn** |
| Frontend | **React + Vite + TypeScript** |
| Short-term memory | In-process ring buffer of recent turns |
| Long-term memory | **SQLite + sentence-transformers** (`all-MiniLM-L6-v2`); revisit a vector DB only if we outgrow it |
| Agent loop | **Hand-built** — JSON tool-call protocol, manual dispatch (no smolagents / LangChain / LangGraph) |
| Package manager | **uv** |
| Python | **3.12** |

## Consequences

**Easier**
- Ollama makes model swaps a one-line config change; no manual GGUF download/convert.
- FastAPI gives us async + WebSocket + Pydantic with very little boilerplate.
- A hand-built agent loop is easy to debug and instrument — exactly what a learning project needs.
- SQLite removes ops complexity: a single file, no server.

**Harder**
- We own the agent-loop edge cases (parsing malformed tool calls, retry policy, tool timeouts) instead of inheriting them from a framework.
- Ollama hides llama.cpp internals — when we want to learn the lower layer (e.g. quantization, KV cache), we'll need to drop down to raw `llama.cpp` later.
- All-Python embeddings will be slow on CPU for large corpora. Acceptable for personal-scale memory; revisit when latency or volume hurts.

## Alternatives Considered

- **Inference**: raw `llama.cpp` (rejected for now — more setup friction; revisit when we want to learn the lower layer) and HuggingFace `transformers` with quantization (rejected — slower than llama.cpp-based runtimes on CPU).
- **Agent framework**: smolagents (rejected — too much abstraction for a learning project) and LangGraph/LangChain (rejected — heavy, opaque, industry-relevant but obscures fundamentals).
- **Memory**: ChromaDB / Qdrant (deferred — overkill at personal scale; revisit if SQLite + cosine search becomes the bottleneck).
- **Frontend**: Next.js (rejected — extra concepts the user doesn't need yet; Vite is the simpler entry point to React).
