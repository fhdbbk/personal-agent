"""Tool registry. Per ADR 0003 §3, this is a dict — not a framework.

Adding a tool is a one-line entry in `TOOLS`. Each tool exposes its name,
description, and JSON-Schema parameters; the per-provider formatters here
wrap that canonical shape into whatever the target LLM expects.
"""

from dataclasses import dataclass
from typing import Awaitable, Callable

from backend.app.tools import fetch_url as fetch_url_mod
from backend.app.tools import list_files as list_files_mod
from backend.app.tools import read_file as read_file_mod
from backend.app.tools import web_search as web_search_mod
from backend.app.tools import write_file as write_file_mod


class ToolError(Exception):
    """A tool raised — the loop turns this into a tool_result with ok=false
    and feeds it back to the model so it can self-correct."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict  # inner JSON Schema for the tool's arguments
    fn: Callable[..., Awaitable[str]]
    requires_approval: bool


def _make(mod, fn, requires_approval: bool) -> Tool:
    return Tool(
        name=mod.NAME,
        description=mod.DESCRIPTION,
        parameters=mod.PARAMETERS,
        fn=fn,
        requires_approval=requires_approval,
    )


TOOLS: dict[str, Tool] = {
    "list_files": _make(list_files_mod, list_files_mod.list_files, False),
    "read_file": _make(read_file_mod, read_file_mod.read_file, False),
    "write_file": _make(write_file_mod, write_file_mod.write_file, True),
    "web_search": _make(web_search_mod, web_search_mod.web_search, False),
    "fetch_url": _make(fetch_url_mod, fetch_url_mod.fetch_url, True),
}


def all_tools() -> list[Tool]:
    return list(TOOLS.values())


# ---- per-provider formatters ---------------------------------------
#
# Ollama and OpenAI share the same "function" tool envelope; Anthropic
# uses a flatter shape with `input_schema` instead of `parameters`.


def ollama_tool_specs(tools: list[Tool]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def openai_tool_specs(tools: list[Tool]) -> list[dict]:
    # Wire-identical to Ollama's; kept as a separate function so the
    # provider call sites read clearly and the shapes can drift later
    # if either side changes.
    return ollama_tool_specs(tools)


def anthropic_tool_specs(tools: list[Tool]) -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in tools
    ]
