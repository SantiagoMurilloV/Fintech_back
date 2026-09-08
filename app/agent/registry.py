"""Tool registry.

A tool is a deterministic Python callable plus a JSON schema. The LLM may only
choose a tool name and arguments; every computation happens inside the tool, so
the numbers in an answer can always be reproduced by replaying the trace.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict            # JSON schema of the arguments
    handler: Callable           # (db: Session, **args) -> ToolResult
    mutates: bool = False       # writes to the database
    examples: list[str] = field(default_factory=list)


@dataclass
class ToolResult:
    """What a tool returns: render blocks plus the raw data it computed."""
    blocks: list[dict]
    data: dict = field(default_factory=dict)
    summary: str = ""


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    if tool.name in _REGISTRY:
        raise ValueError(f"Tool duplicada: {tool.name}")
    _REGISTRY[tool.name] = tool
    return tool


def tool(name: str, description: str, parameters: dict, mutates: bool = False,
         examples: list[str] | None = None):
    """Decorator that registers a handler as a tool."""
    def wrapper(handler: Callable) -> Callable:
        register(Tool(name=name, description=description, parameters=parameters,
                      handler=handler, mutates=mutates, examples=examples or []))
        return handler
    return wrapper


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def all_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def schemas() -> list[dict]:
    """OpenAI-style function schemas, used when the LLM planner is consulted."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in _REGISTRY.values()
    ]


def execute(name: str, db: Session, arguments: dict) -> ToolResult:
    selected = get(name)
    if selected is None:
        raise KeyError(f"La herramienta «{name}» no existe.")
    return selected.handler(db, **arguments)
