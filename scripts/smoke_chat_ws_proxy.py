"""Verify the Vite dev proxy correctly upgrades the WebSocket to FastAPI."""

import asyncio
import json
import sys

import websockets


async def main() -> int:
    async with websockets.connect("ws://localhost:5173/chat/stream") as ws:
        await ws.send(json.dumps({"conversation_id": "proxy-smoke", "message": "ping"}))
        async for raw in ws:
            frame = json.loads(raw)
            if frame["type"] == "done":
                print("ok: WS proxy works end-to-end")
                return 0
            if frame["type"] == "error":
                print(f"error: {frame['error']}", file=sys.stderr)
                return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
