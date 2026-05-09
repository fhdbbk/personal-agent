# 2026-05-10 — `web_search` tool + ADR 0005

## Goal

Pick up the next item on the previous session's handoff: **Phase 2 second half — add `web_search` and `python_exec`.** Scope this session to `web_search` only (per the recommendation, defer `python_exec` to a focused session for the sandboxing). Also document the message-journey/async learnings from earlier in the day before starting.

## What we did

1. **Walked the message journey end-to-end (async focus).** Read `api/chat.py`, `agent/loop.py`, `frontend/src/App.tsx`, `tools/registry.py`, `memory/buffer.py` together; traced the path from React composer → WS frame → `chat_stream` coroutine → `run_turn` → streaming Ollama call → tool dispatch with inline approval round-trip → loop exit. Captured the consequences of "one event loop, one coroutine per WS, sequential per turn." Side question: why we call the conversation buffer a *ring buffer* even though Python `deque` is a doubly-linked list of blocks (it's the bounded-FIFO-with-eviction *behavior*, not the data structure).
2. **Wrote [docs/learnings/message-journey-async.md](../learnings/message-journey-async.md).** Headline + per-hop walkthrough + the approval round-trip trick + ring-buffer naming aside + "things that would break this single-loop model" + async cheat-sheet + pitfalls. Cross-linked to the existing [agent-loop-architecture.md](../learnings/agent-loop-architecture.md) and [streaming-with-tools.md](../learnings/streaming-with-tools.md) so the three together cover the loop and its surroundings without overlap.
3. **Aligned on three `web_search` choices via AskUserQuestion.** Tool order: `web_search` first, defer `python_exec`. Parser: selectolax (fast C-backed). Result count: top 5 fixed (no `count` arg in the schema).
4. **First implementation: raw HTML scrape.** Wrote [`backend/app/tools/web_search.py`](../../backend/app/tools/web_search.py) using `httpx.AsyncClient` POST against `html.duckduckgo.com/html/`, with selectolax CSS selectors on `.result__a` + `.result__snippet`, and a `_unwrap_ddg_redirect` helper to extract the inner URL from DDG's `/l/?uddg=…` wrapper. Registered in `tools/registry.py` (`requires_approval=False`).
5. **Wrote [`scripts/smoke_web_search.py`](../../scripts/smoke_web_search.py)** — direct three-query probe with no agent or LLM in the loop. Fail-loud on zero results.
6. **Hit DDG bot-detection immediately.** First query `'fastapi websocket tutorial'` returned 5 clean results. The next two returned `No results`. Probing the raw response showed **HTTP 202** with `cc=botnet` in a hidden URL — the bot-challenge page. Tried richer browser-like headers (full Firefox UA, `Accept-Language`, `Referer`, `Origin`, etc.), then a homepage warm-up to seed cookies, then GET vs POST, then the `lite` endpoint. All returned 202. Even the previously-working query failed once the rate-limiter latched on.
7. **Picked the fallback per ADR 0003 §5: `ddgs` library.** AskUserQuestion offered ddgs / Brave Search API / ship-as-flaky / SearXNG; user chose ddgs. Probed `ddgs.text(...)` from the same IP that was blocked seconds earlier — instantly returned 5 results with title/href/body. The library uses `primp` (Rust HTTP client with full browser TLS-fingerprint emulation) which defeats DDG's bot detector at a layer below HTTP headers.
8. **Refactored [`web_search.py`](../../backend/app/tools/web_search.py)** to ~30 lines: `DDGS().text(query, max_results=5)` wrapped in `asyncio.to_thread`, plus a small formatter. Removed the `_unwrap_ddg_redirect` helper, the selectolax import, the httpx import. Removed `selectolax` and `httpx` from pyproject deps (`uv remove`); ddgs brings `lxml` + `primp` as transitives. Net +1 effective dep.
9. **Re-ran [`scripts/smoke_web_search.py`](../../scripts/smoke_web_search.py)** — all three queries returned clean top-5. No flakiness.
10. **End-to-end smoke through the agent.** Wrote [`scripts/smoke_agent_web_search.py`](../../scripts/smoke_agent_web_search.py) — opens the WS, sends "Use the web_search tool to find what FastAPI WebSockets are, then summarise in one sentence." Asserts a `web_search` tool_call appears in the frame stream. Ran with uvicorn in the background; agent rewrote the query to `"FastAPI WebSockets definition and usage"`, called web_search, got real results, summarised in one sentence. Passed.
11. **Regression check on the existing tools.** Ran [`scripts/smoke_agent.py`](../../scripts/smoke_agent.py) — all three turns (read_file, write_file approve, write_file deny) still pass. The registry change is additive; nothing broke.
12. **Wrote [ADR 0005](../decisions/0005-search-backend-ddgs.md).** Captures the swap from raw scrape to `ddgs`, why the scrape failed (TLS fingerprinting beats header tricks), why ddgs works (`primp`), what we kept (schema, approval flag, async-via-`to_thread`), and the next-fallback story (Brave Search API, SearXNG).
13. **Updated [CLAUDE.md](../../CLAUDE.md)** — added ADR 0005 to the decisions table, added `web_search.py` to the repo-layout tree, added the two new smoke scripts, added them to the Commands section, and updated the "future phases will add" line to reflect that `web_search` is no longer future.

## Decisions made

- [ADR 0005](../decisions/0005-search-backend-ddgs.md) — `web_search` uses the `ddgs` library, not raw HTML scraping. ADR 0003 §5 anticipated this exact swap as a fallback path; we took it on the first day the raw scrape was tried.
- **Did not write an ADR for the parser choice or the result count.** Both were ephemeral pre-decisions made via AskUserQuestion; selectolax is now removed and the result count (5) is a constant in the tool, not a runtime config. If we ever expose `count` as a per-call arg or move to a different formatter, *that* would deserve a note.

## Snags + fixes

- **Raw DDG scrape is bot-blocked at the TLS-fingerprint layer.** Headers-tier mitigations (Accept-Language, Referer, homepage warm-up to seed cookies, GET vs POST, `lite` vs `html` endpoint) all returned the same HTTP 202 + bot-challenge page. The fix wasn't to layer on more headers but to switch HTTP clients entirely (httpx → primp via `ddgs`). **Lesson:** when a server's rate-limiter is built on TLS/HTTP/2 fingerprinting, it's looking at things below the `httpx` API surface; you can't paper over it with header changes.
- **The earlier "first query worked" was misleading.** Made me think headers might be enough. It wasn't — it was a fresh-IP grace period that ran out. **Lesson:** when probing a bot-detected endpoint, don't celebrate the first success; keep going until the failure mode shows up.
- **Removed `selectolax` + `httpx` direct deps after the refactor.** Easy to forget; followed YAGNI before committing.

## Open threads / next session

- **`python_exec` — the other half of Phase 2.** This is the bigger lift: subprocess + `RLIMIT_CPU` + `RLIMIT_AS` + sandbox-dir chdir + no-network (HTTP_PROXY trick or `unshare`). Requires approval per call. Plan to write a learnings doc on Linux rlimits as we go (called out in the previous session log too).
- **Unit tests on the agent loop.** Standing item, untouched. The LLD's sequence diagrams in [docs/design/LLD.md](../design/LLD.md) are still the right test fixtures.
- **Streamed mid-iteration `tool_calls` deltas.** Carried; only matters if a future Ollama version splits tool_calls across chunks.
- **UI papercuts** (long tool args/results display, "Running…" indicator). Carried.
- **Pick a real long-term-memory model before Phase 3.** Carried; verify `all-MiniLM-L6-v2` is still right when Phase 3 starts.
- **Verify Mermaid renders in VS Code preview.** Carried from the previous session.
- **Carried forward (still on Fahad):** phone browser smoke test, disable Windows Ollama autostart, DHCP reservation, SOUL.md rewrite in own voice.
- **Watch `ddgs` for breakage.** The library is the new fragile spot. If/when it stops returning results, the documented fallback is Brave Search API (free tier, `PA_BRAVE_API_KEY`), and the swap is local to `web_search.py` — schema and approval flag are the contract.

## Out-of-band notes

- The decisions table in CLAUDE.md is starting to feel busy. If we add three or four more rows, consider folding "agent loop" entries into a single "agent loop" subsection of the table or moving the table out of CLAUDE.md and into [docs/design/HLD.md](../design/HLD.md). Don't act on this preemptively — it's still readable.
- `primp` is a Rust binary wheel via `ddgs`. First time we've taken a Rust HTTP client transitively. Wheels are available for Linux/Mac/Windows desktops; if mobile (Phase 7) ever needs this code, revisit.
- The session also produced [docs/learnings/message-journey-async.md](../learnings/message-journey-async.md) earlier; that's a synthesis doc, not a debugging note, but it earns its own file because the topic spans the entire pipeline and the existing [agent-loop-architecture.md](../learnings/agent-loop-architecture.md) and [streaming-with-tools.md](../learnings/streaming-with-tools.md) didn't cover the *async* shape end-to-end.
