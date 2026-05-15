"""Smoke-test the /chat/stream WebSocket: send one turn, print tokens as they arrive."""

import asyncio
import json
import sys
import time

import websockets


async def main() -> int:
    url = "ws://127.0.0.1:8000/chat/stream"
    payload = {
        "conversation_id": "ws-smoke",
        "message": "Count from 1 to 5, separated by commas. No other text.",
    }
    t0 = time.perf_counter()
    first_token_at: float | None = None
    chunks: list[str] = []

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(payload))
        async for raw in ws:
            frame = json.loads(raw)
            ftype = frame.get("type")
            if ftype == "token":
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                chunks.append(frame["delta"])
                sys.stdout.write(frame["delta"])
                sys.stdout.flush()
            elif ftype == "done":
                stats = frame.get("stats")
                if stats:
                    print(f"\n[stats] {stats}")
                break
            elif ftype == "error":
                print(f"\n[error] {frame.get('error')}", file=sys.stderr)
                return 1

    end = time.perf_counter()
    print()
    ttft_ms = int((first_token_at - t0) * 1000) if first_token_at else -1
    total_ms = int((end - t0) * 1000)
    print(f"\nttft={ttft_ms}ms  total={total_ms}ms  chars={sum(len(c) for c in chunks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
