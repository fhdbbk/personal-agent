"""End-to-end smoke: agent uses web_search then fetch_url.

The motivating case from ADR 0006: search snippets are not enough for
live data, so the agent should follow up with fetch_url on a relevant
hit. We auto-approve the fetch_url call (it's approval-gated).

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
CID = f"smoke-fetch-{int(time.time())}"


async def drive_turn(ws, message: str) -> None:
    print(f"\n>>> user: {message}")
    await ws.send(json.dumps({"conversation_id": CID, "message": message}))
    final_chunks: list[str] = []
    saw_search = False
    saw_fetch = False
    while True:
        raw = await ws.recv()
        frame = json.loads(raw)
        t = frame.get("type")
        if t == "token":
            final_chunks.append(frame["delta"])
        elif t == "tool_call":
            print(f"  [tool_call] {frame['name']}({frame['args']})")
            if frame["name"] == "web_search":
                saw_search = True
            elif frame["name"] == "fetch_url":
                saw_fetch = True
        elif t == "tool_result":
            ok = "ok" if frame["ok"] else "ERR"
            preview = frame["preview"][:300].replace("\n", " | ")
            print(f"  [tool_result {ok}] {preview}")
        elif t == "tool_approval":
            print(f"  [tool_approval] {frame['name']}({frame['args']}) -> approve")
            await ws.send(
                json.dumps(
                    {
                        "type": "approval_response",
                        "call_id": frame["call_id"],
                        "approved": True,
                    }
                )
            )
        elif t == "done":
            final = "".join(final_chunks).strip()
            print(f"<<< assistant: {final[:600]}")
            if not saw_search:
                print("!!! FAIL: agent did not call web_search", file=sys.stderr)
                sys.exit(2)
            if not saw_fetch:
                print("!!! FAIL: agent did not call fetch_url", file=sys.stderr)
                sys.exit(2)
            return
        elif t == "error":
            print(f"!!! error: {frame['error']}", file=sys.stderr)
            sys.exit(1)


async def main() -> None:
    async with websockets.connect(URL) as ws:
        await drive_turn(
            ws,
            "Use web_search to find the Wikipedia article on the Pound sterling, "
            "then use fetch_url on the wikipedia.org link to read it, and "
            "summarise in one sentence what the pound is.",
        )


if __name__ == "__main__":
    asyncio.run(main())
