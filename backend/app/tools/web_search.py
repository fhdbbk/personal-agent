"""DuckDuckGo search via the `ddgs` library — Phase 2 tool.

We tried a raw HTML scrape first (httpx + selectolax against
html.duckduckgo.com); ADR 0003 §5 picked DDG for "no API key, easy to
swap" but the raw scrape gets HTTP 202 + bot-challenge after a couple of
calls from the same IP. `ddgs` solves that with browser-fingerprinted
requests and endpoint rotation, so we delegate to it. The library is
sync; we run it in a thread so the event loop keeps moving. See
[ADR 0005](docs/decisions/0005-search-backend-ddgs.md) for the full
rationale.
"""

import asyncio
import logging

from ddgs import DDGS

log = logging.getLogger("pa.tool.web_search")

MAX_RESULTS = 5

SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web via DuckDuckGo and return the top results as a "
            "numbered list with title, URL, and snippet. Use this when the "
            "answer needs information that is not in the conversation, the "
            "sandbox, or your training data — for example current events, "
            "recent releases, or facts you are unsure about."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Plain-text search query.",
                },
            },
            "required": ["query"],
        },
    },
}


def _search_sync(query: str, count: int) -> list[dict[str, str]]:
    with DDGS() as ddgs:
        # ddgs.text yields dicts with keys: title, href, body.
        return list(ddgs.text(query, max_results=count))


def _format_results(query: str, results: list[dict[str, str]]) -> str:
    if not results:
        return f"No results for {query!r}."
    lines = [f"Search results for {query!r}:"]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("href") or "").strip()
        snippet = (r.get("body") or "").strip()
        lines.append(f"\n{i}. {title}")
        lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


async def web_search(query: str) -> str:
    results = await asyncio.to_thread(_search_sync, query, MAX_RESULTS)
    log.info("web_search q=%r got=%d", query, len(results))
    return _format_results(query, results)
