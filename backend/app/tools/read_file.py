import asyncio

from backend.app.tools._sandbox import safe_path

MAX_BYTES = 64_000  # ~16k tokens at 4 chars/token; keeps a single tool result well under the model's context.


SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a UTF-8 text file from the agent sandbox. Use this to "
            "inspect files the user placed there or that earlier tool calls "
            "created. Returns the file contents as a string."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the sandbox root.",
                },
            },
            "required": ["path"],
        },
    },
}


async def read_file(path: str) -> str:
    p = safe_path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such file: {path}")
    if not p.is_file():
        raise IsADirectoryError(f"not a regular file: {path}")
    data = await asyncio.to_thread(p.read_bytes)
    if len(data) > MAX_BYTES:
        # Truncate so the model can still see *something* and decide what to do.
        text = data[:MAX_BYTES].decode("utf-8", errors="replace")
        return f"{text}\n\n[... truncated; file is {len(data)} bytes, showing first {MAX_BYTES}]"
    return data.decode("utf-8", errors="replace")
