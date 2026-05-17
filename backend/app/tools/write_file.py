import asyncio

from backend.app.tools._sandbox import safe_path


NAME = "write_file"
DESCRIPTION = (
    "Write a UTF-8 text file to the agent sandbox, creating parent "
    "directories as needed. Overwrites existing files. Requires user "
    "approval."
)
PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path relative to the sandbox root.",
        },
        "content": {
            "type": "string",
            "description": "Full file contents to write.",
        },
    },
    "required": ["path", "content"],
}


async def write_file(path: str, content: str) -> str:
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(p.write_text, content, "utf-8")
    return f"wrote {len(content)} chars to {path}"
