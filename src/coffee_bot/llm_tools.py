"""Minimal tool definitions for OpenAI-compatible chat APIs (OpenRouter).

`@tool` turns an async function into a Tool: the JSON schema comes from the type hints, and
parameter descriptions come from the Google-style "Args:" section of the docstring.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, get_type_hints

from pydantic import ConfigDict, create_model


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable[..., Awaitable[str]]
    model: Any  # pydantic model used to validate/coerce arguments

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.schema},
        }

    async def call(self, arguments: dict) -> str:
        parsed = self.model.model_validate(arguments)
        kwargs = {name: getattr(parsed, name) for name in parsed.model_fields_set}
        return await self.fn(**kwargs)


def _split_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """Split a docstring into its description and the per-parameter text from its "Args:" section."""
    head, _, args_block = inspect.cleandoc(doc).partition("\nArgs:\n")
    descriptions: dict[str, str] = {}
    current: str | None = None
    for line in args_block.splitlines():
        m = re.match(r"^ {1,4}(\w+): (.*)$", line)  # entries are indented one level; continuations deeper
        if m:
            current = m.group(1)
            descriptions[current] = m.group(2).strip()
        elif current and line.strip():
            descriptions[current] += " " + line.strip()
    return head.strip(), descriptions


def _clean(node: Any) -> Any:
    """Drop pydantic's auto-generated titles; they only add noise for the model."""
    if isinstance(node, dict):
        return {k: _clean(v) for k, v in node.items() if k != "title" or isinstance(v, dict)}
    if isinstance(node, list):
        return [_clean(v) for v in node]
    return node


def tool(fn: Callable[..., Awaitable[str]]) -> Tool:
    description, arg_docs = _split_docstring(fn.__doc__ or fn.__name__)
    hints = get_type_hints(fn)
    fields: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (hints[name], default)
    model = create_model(fn.__name__, __config__=ConfigDict(extra="forbid"), **fields)
    schema = _clean(model.model_json_schema())
    for name, text in arg_docs.items():
        if name in schema.get("properties", {}):
            schema["properties"][name]["description"] = text
    return Tool(name=fn.__name__, description=description, schema=schema, fn=fn, model=model)
