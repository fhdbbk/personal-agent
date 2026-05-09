"""End-to-end smoke: drive the agent through a web_search turn.

Run uvicorn separately:
  uv run uvicorn backend.app.main:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import websockets


URL = "ws://127.0.0.1:8000/chat/stream"
CID = f"smoke-web-{int(time.time())}"


async def drive_turn(ws, message: str) -> None:
    print(f"\n>>> user: {message}")
    await ws.send(json.dumps({"conversation_id": CID, "message": message}))
    final_chunks: list[str] = []
    saw_web_search = False
    while True:
        raw = await ws.recv()
        frame = json.loads(raw)
        t = frame.get("type")
        if t == "token":
            final_chunks.append(frame["delta"])
        elif t == "tool_call":
            print(f"  [tool_call] {frame['name']}({frame['args']})")
            if frame["name"] == "web_search":
                saw_web_search = True
        elif t == "tool_result":
            ok = "ok" if frame["ok"] else "ERR"
            preview = frame["preview"][:300].replace("\n", " | ")
            print(f"  [tool_result {ok}] {preview}")
        elif t == "done":
            final = "".join(final_chunks).strip()
            print(f"<<< assistant: {final[:500]}")
            if not saw_web_search:
                print("!!! FAIL: agent did not call web_search", file=sys.stderr)
                sys.exit(2)
            return
        elif t == "error":
            print(f"!!! error: {frame['error']}", file=sys.stderr)
            sys.exit(1)


async def main() -> None:
    async with websockets.connect(URL) as ws:
        await drive_turn(
            ws,
            "Use the web_search tool to find what FastAPI WebSockets are, "
            "then summarise in one sentence.",
        )


if __name__ == "__main__":
    asyncio.run(main())
