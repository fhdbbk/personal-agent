"""Smoke-test the Phase 2 agent loop over the WS protocol.

Walks two turns:
  1. "What's in notes.txt?" — should trigger read_file (no approval).
  2. "Write a file called greeting.txt with 'hello world'" — should trigger
     write_file (approval required). We auto-approve.

Run uvicorn separately first:
  uv run uvicorn backend.app.main:app --port 8000
"""

import asyncio
import json
import sys
import time

import websockets


URL = "ws://127.0.0.1:8000/chat/stream"
CID = f"smoke-agent-{int(time.time())}"


async def drive_turn(ws, message: str, *, auto_approve: bool = True) -> None:
    print(f"\n>>> user: {message}")
    await ws.send(json.dumps({"conversation_id": CID, "message": message}))
    final_chunks: list[str] = []
    while True:
        raw = await ws.recv()
        frame = json.loads(raw)
        t = frame.get("type")
        if t == "token":
            final_chunks.append(frame["delta"])
        elif t == "tool_call":
            print(f"  [tool_call] {frame['name']}({frame['args']}) call_id={frame['call_id']}")
        elif t == "tool_result":
            ok = "ok" if frame["ok"] else "ERR"
            preview = frame["preview"][:200].replace("\n", " ")
            print(f"  [tool_result {ok}] {preview}")
        elif t == "tool_approval":
            decision = "approve" if auto_approve else "deny"
            print(f"  [tool_approval] {frame['name']}({frame['args']}) -> {decision}")
            await ws.send(
                json.dumps(
                    {
                        "type": "approval_response",
                        "call_id": frame["call_id"],
                        "approved": auto_approve,
                    }
                )
            )
        elif t == "done":
            final = "".join(final_chunks)
            print(f"<<< assistant: {final.strip()}")
            return
        elif t == "error":
            print(f"!!! error: {frame['error']}")
            sys.exit(1)
        else:
            print(f"  [unknown frame] {frame}")


async def main() -> None:
    async with websockets.connect(URL) as ws:
        await drive_turn(ws, "What's in the file notes.txt? Use the read_file tool.")
        await drive_turn(
            ws,
            "Now write a file called greeting.txt with the content 'hello world'. Use write_file.",
            auto_approve=True,
        )
        await drive_turn(
            ws,
            "Write a file named blocked.txt containing the single word: nope.",
            auto_approve=False,
        )


if __name__ == "__main__":
    asyncio.run(main())
