# 0005 — `web_search` backend: `ddgs` library, not raw HTML scrape

**Status:** Accepted
**Date:** 2026-05-10
**Phase:** 2

## Context

[ADR 0003 §5](0003-agent-loop.md) committed `web_search` to a "DuckDuckGo HTML scrape — no API key, easy to swap" and explicitly listed SearXNG and Brave Search API as fallbacks. When we shipped the raw scrape (httpx + selectolax against `html.duckduckgo.com/html/`) it worked for one query and then DDG started returning **HTTP 202 + a bot-challenge page** (`cc=botnet` in the response URL) for everything from the same IP. Browser-like headers, the `lite` endpoint, GET vs POST, homepage warm-up to seed cookies — none of it changed the outcome once the rate-limiter latched on.

We need a search backend that holds up under repeated calls without manual key signups. ADR 0003 anticipated this exact failure.

## Decision

Use the [`ddgs`](https://pypi.org/project/ddgs/) library as the `web_search` backend. The tool stays:

- **Same name and schema** — `web_search(query)` returns a numbered list of `title / url / snippet`.
- **Same approval policy** — read-only, no approval required.
- **Same async wrapper** — `ddgs.text` is sync; we run it via `asyncio.to_thread`.

What changed inside the tool: ~80 lines of httpx + selectolax + redirect-unwrap + result-block CSS scraping is now ~3 lines wrapping `DDGS().text(query, max_results=5)`.

## Why `ddgs` and not the alternatives

- **Brave Search API** — clean from-scratch HTTP, but requires an API key (free tier 2000 queries/month at api.search.brave.com). Adds an external account to the project's setup story. Worth keeping as the next fallback, not the first.
- **SearXNG self-hosted** — heaviest setup. Self-host a meta-search engine, query its JSON API. No API keys, no scraping, but it adds a second long-running service to a "runs on a laptop" project. Defer until the day we want metasearch + result re-ranking and the ops cost is justified.
- **Keep raw scrape, accept fragility** — the parse path was correct and the failure mode is just "No results", which the model can sort of work around. But ~half of demos broken is too high a tax for a tool that's supposed to demonstrate the loop.
- **`duckduckgo_search` (the older library)** — deprecated by upstream; renamed/forked to `ddgs`. Don't use the old name.

`ddgs` wins on three axes: it works without an API key, it's a single dependency add, and our integration is small enough to swap later — the `web_search(query) -> str` signature is the contract; everything inside is replaceable.

## How `ddgs` solves the bot challenge

`ddgs` ships with [`primp`](https://pypi.org/project/primp/), a Rust HTTP client that emulates a real browser's TLS fingerprint (JA3/JA4) and HTTP/2 frame ordering. DDG's bot detector classifies httpx requests as automated based on those fingerprints, not just headers. `primp` defeats the classifier in a way that adding `Accept-Language` headers cannot. The library also rotates through DDG's various endpoints (`html`, `lite`, etc.) on retry. Both probed working from our IP after raw scrape was firmly blocked.

This is exactly the kind of detail that reinventing has no educational value — it's an arms race against TLS fingerprinting, not a learning exercise about agent loops.

## Consequences

**Easier**
- The tool is now ~80 lines simpler. Roughly: build `DDGS`, call `.text`, format results.
- Reliability improved enough that smoke tests pass repeatably.
- Two deps (`selectolax`, `httpx`) are dropped from the project; `ddgs` brings `lxml` and `primp` as transitive deps. Net +1 effective dep.
- The `ddgs` library handles endpoint rotation and retries — we don't write retry logic ourselves.

**Harder**
- We've moved one notch away from the "from-scratch" goal. The HTTP transport and the HTML parsing are now in a library, not in our code. The educational locus narrows to "wrap a sync library with `asyncio.to_thread`, format results for an LLM, register a tool" — still meaningful for the learning project but smaller than originally scoped.
- We inherit `ddgs`'s release cadence and bugs. If DDG further locks down and `ddgs` doesn't keep up, we move to Brave Search API.
- `primp` is a Rust binary wheel — adds platform-specific install footprint. Not a concern on Linux/Mac/Windows desktops; would matter if we ever target really constrained mobile.

## Alternatives reconsidered

- **Brave Search API** — kept as the documented next fallback. If `ddgs` itself starts failing, we add `PA_BRAVE_API_KEY` and switch the tool body. The schema and approval flag are unchanged.
- **A retry+backoff layer over raw scrape** — rejected. The bot detector returns a 200-ish response with a challenge page; retrying the same request just gets the same challenge.

## Open questions / risks

- **DDG could deprecate the endpoints `ddgs` uses.** No upstream contract — same risk profile as the raw scrape, just better-amortised because the library will probably patch faster than we would.
- **`primp` is unfamiliar.** First time we've taken a Rust HTTP client as a transitive dep. If a wheel ever fails to install on a target platform, we have to think about it.
- **Content quality.** `ddgs.text` returns title/href/body; the body field is sometimes a clean snippet, sometimes a chunk of page text. We pass it through unchanged; if the model trips on long bodies we'll truncate per-result.

## What this doesn't change

- ADR 0003 stays accepted; this is the §5-anticipated swap, not an override.
- The wire protocol from ADR 0002/0004 — the `tool_call` / `tool_result` frames — is identical. The UI code does not change.
- The agent loop in `loop.py` does not change. Tools are still `Callable[..., Awaitable[str]]` and `web_search` still has `requires_approval=False`.
