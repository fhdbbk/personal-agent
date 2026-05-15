"""List directory contents inside the agent sandbox — Phase 2 tool.

Pairs with read_file / write_file: the agent often needs to see what
exists before deciding what to read. Non-recursive on purpose — a
recursive listing of a deep tree dwarfs the file content the model
actually wants. If the model needs to descend, it calls list_files
again with a sub-path.
"""

from __future__ import annotations

import asyncio

from backend.app.tools._sandbox import safe_path

MAX_ENTRIES = 200  # cap so a runaway directory doesn't blow up the context window


SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": (
            "List the entries in a directory inside the agent sandbox. "
            "Use this to discover what files exist before reading them. "
            "Returns a sorted listing (directories first, then files) "
            "with sizes. Non-recursive — pass a sub-path to descend."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path relative to the sandbox root. Use \".\" "
                        "for the sandbox root itself."
                    ),
                },
            },
            "required": ["path"],
        },
    },
}


def _list_sync(target_path) -> list:
    return list(target_path.iterdir())


def _format(entries: list, rel: str) -> str:
    dirs = sorted([e for e in entries if e.is_dir()], key=lambda p: p.name.lower())
    files = sorted([e for e in entries if e.is_file()], key=lambda p: p.name.lower())
    other = sorted(
        [e for e in entries if not e.is_dir() and not e.is_file()],
        key=lambda p: p.name.lower(),
    )

    lines = [f"Listing of {rel!r}:"]
    if not entries:
        lines.append("  (empty)")
        return "\n".join(lines)

    for d in dirs:
        lines.append(f"  {d.name}/  (dir)")
    for f in files:
        try:
            size = f.stat().st_size
            lines.append(f"  {f.name}  (file, {size} bytes)")
        except OSError:
            lines.append(f"  {f.name}  (file, ?)")
    for o in other:
        lines.append(f"  {o.name}  (other)")
    return "\n".join(lines)


async def list_files(path: str) -> str:
    p = safe_path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such path: {path}")
    if not p.is_dir():
        raise NotADirectoryError(f"not a directory: {path}")

    entries = await asyncio.to_thread(_list_sync, p)
    truncated = ""
    if len(entries) > MAX_ENTRIES:
        truncated = (
            f"\n\n[... truncated; directory has {len(entries)} entries, "
            f"showing first {MAX_ENTRIES}]"
        )
        entries = entries[:MAX_ENTRIES]

    return _format(entries, path) + truncated
