"""Tool registry. Per ADR 0003 §3, this is a dict — not a framework.

Adding a tool is a one-line entry in `TOOLS`. The `Tool` dataclass holds
the callable, the JSON schema we hand to Ollama, and the approval flag.
"""

from dataclasses import dataclass
from typing import Awaitable, Callable

from backend.app.tools.fetch_url import SCHEMA as FETCH_URL_SCHEMA
from backend.app.tools.fetch_url import fetch_url
from backend.app.tools.list_files import SCHEMA as LIST_FILES_SCHEMA
from backend.app.tools.list_files import list_files
from backend.app.tools.read_file import SCHEMA as READ_FILE_SCHEMA
from backend.app.tools.read_file import read_file
from backend.app.tools.web_search import SCHEMA as WEB_SEARCH_SCHEMA
from backend.app.tools.web_search import web_search
from backend.app.tools.write_file import SCHEMA as WRITE_FILE_SCHEMA
from backend.app.tools.write_file import write_file


class ToolError(Exception):
    """A tool raised — the loop turns this into a tool_result with ok=false
    and feeds it back to the model so it can self-correct."""


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., Awaitable[str]]
    schema: dict
    requires_approval: bool


TOOLS: dict[str, Tool] = {
    "list_files": Tool(
        name="list_files",
        fn=list_files,
        schema=LIST_FILES_SCHEMA,
        requires_approval=False,
    ),
    "read_file": Tool(
        name="read_file",
        fn=read_file,
        schema=READ_FILE_SCHEMA,
        requires_approval=False,
    ),
    "write_file": Tool(
        name="write_file",
        fn=write_file,
        schema=WRITE_FILE_SCHEMA,
        requires_approval=True,
    ),
    "web_search": Tool(
        name="web_search",
        fn=web_search,
        schema=WEB_SEARCH_SCHEMA,
        requires_approval=False,
    ),
    "fetch_url": Tool(
        name="fetch_url",
        fn=fetch_url,
        schema=FETCH_URL_SCHEMA,
        requires_approval=True,
    ),
}


def ollama_tool_specs() -> list[dict]:
    """The list of schemas to pass to `ollama.chat(tools=...)`."""
    return [t.schema for t in TOOLS.values()]
