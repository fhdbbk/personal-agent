"""Smoke-test the web_search tool directly (no agent loop, no LLM).

Confirms the DDG HTML scrape path works and the parser is finding results.
Fails loud if zero results come back — DDG occasionally rewrites their
HTML, and silent zero-result returns are the main breakage mode.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.tools.web_search import web_search


QUERIES = [
    "fastapi websocket tutorial",
    "qwen3 model card",
    "duckduckgo html endpoint",
]


async def main() -> int:
    bad = 0
    for q in QUERIES:
        print(f"\n=== {q!r} ===")
        try:
            result = await web_search(q)
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}", file=sys.stderr)
            bad += 1
            continue
        print(result)
        if result.startswith("No results"):
            print("  FAIL: zero results", file=sys.stderr)
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
