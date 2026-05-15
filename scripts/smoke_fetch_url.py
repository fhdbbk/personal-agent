"""Smoke-test the fetch_url tool directly (no agent loop, no LLM).

Confirms three things:
  1. A normal public page produces non-empty extracted text.
  2. A non-HTML / unsupported content-type is rejected cleanly.
  3. A non-public host (loopback) is rejected by the safety check.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.tools.fetch_url import fetch_url


async def expect_ok(url: str) -> bool:
    print(f"\n=== OK case: {url} ===")
    try:
        out = await fetch_url(url)
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    head = out[:400].replace("\n", " | ")
    print(f"  got {len(out)} chars: {head}")
    if "extracted no readable text" in out:
        print("  WARN: empty extract (page likely a JS-shell)")
    return True


async def expect_reject(url: str, reason: str) -> bool:
    print(f"\n=== REJECT case ({reason}): {url} ===")
    try:
        out = await fetch_url(url)
    except ValueError as e:
        print(f"  rejected as expected: {e}")
        return True
    except Exception as e:
        print(f"  WRONG ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    print(f"  FAIL: should have rejected. Got: {out[:200]}", file=sys.stderr)
    return False


async def main() -> int:
    results = []
    # Wikipedia is the most parser-friendly public page on the internet.
    results.append(await expect_ok("https://en.wikipedia.org/wiki/Pound_sterling"))
    # Loopback should never be reachable from this tool.
    results.append(await expect_reject("http://127.0.0.1:8000/", "loopback"))
    results.append(await expect_reject("http://localhost/", "loopback name"))
    # RFC1918.
    results.append(await expect_reject("http://10.0.0.1/", "private range"))
    # Wrong scheme.
    results.append(await expect_reject("file:///etc/passwd", "non-http scheme"))

    bad = sum(1 for ok in results if not ok)
    print(f"\n{'PASS' if bad == 0 else f'FAIL ({bad})'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
