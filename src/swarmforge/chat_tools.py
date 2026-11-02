"""Generic tool loop for the chatbot.

`ToolRegistry` holds named tools with JSON-schema parameters and executes them
without ever raising: errors are returned as ``ERROR: ...`` strings so the LLM
can see and react to them. `chat_with_tools` runs a multi-round conversation in
which the model may call tools until it produces a plain answer.

This module is intentionally dependency-free (no swarmforge.tools imports) and
duck-types the LLM client: anything with an async
``chat(role, messages, *, tools=None, task_key=None)`` returning an object with
a ``.message`` dict works.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

Handler = Callable[..., str | Awaitable[str]]


@dataclass
class ToolSpec:
    """A registered tool: name, description, JSON-schema parameters and handler."""

    name: str
    description: str
    parameters: dict
    handler: Handler


class ToolRegistry:
    """Registry of tools the LLM can call.

    `execute` never raises: unknown tools and handler exceptions are converted
    into ``ERROR: ...`` strings so the model can react to them in the loop.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self, name: str, description: str, parameters: dict, handler: Handler
    ) -> None:
        """Register (or replace) a tool by name."""
        self._tools[name] = ToolSpec(name, description, parameters, handler)

    def names(self) -> list[str]:
        """Registered tool names, in registration order."""
        return list(self._tools)

    def schemas(self) -> list[dict]:
        """OpenAI function schemas for all registered tools."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    async def execute(self, name: str, args: dict) -> str:
        """Run a tool by name; never raises, errors become ERROR strings."""
        if name not in self._tools:
            return f"ERROR: unknown tool '{name}'. Available: {', '.join(self.names())}"
        try:
            result = self._tools[name].handler(**args)
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:  # noqa: BLE001 - deliberate: never raise to the loop
            return f"ERROR: {type(e).__name__}: {e}"
        if inspect.isawaitable(result):
            try:
                result = await result
            except TypeError as e:
                return f"ERROR: bad arguments for {name}: {e}"
            except Exception as e:  # noqa: BLE001
                return f"ERROR: {type(e).__name__}: {e}"
        return str(result)


async def chat_with_tools(
    llm: Any,
    role: str,
    messages: list[dict],
    registry: ToolRegistry,
    *,
    max_rounds: int = 4,
    task_key: str = "chatbot",
    trace: list | None = None,
) -> str:
    """Run a tool-calling conversation loop and return the final text answer.

    The caller's `messages` list is never mutated. If the model is still calling
    tools after `max_rounds` rounds, one final call without tools asks it to
    answer with what it has.

    `trace` is an optional list the caller supplies; when given, one entry
    `{"name", "args", "ok", "preview"}` is appended after each executed tool
    call (ok=False when the tool returned an ERROR string). Behavior is
    unchanged when trace is None.
    """
    if max_rounds < 1:
        raise ValueError("max_rounds must be >= 1")
    work = list(messages)
    for _ in range(max_rounds):
        resp = await llm.chat(role, work, tools=registry.schemas(), task_key=task_key)
        work.append(
            {
                "role": "assistant",
                "content": resp.message.get("content"),
                "tool_calls": resp.message.get("tool_calls"),
            }
        )
        tool_calls = resp.message.get("tool_calls") or []
        if not tool_calls:
            return resp.message.get("content") or ""
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments") or ""
            try:
                args = json.loads(raw_args) if raw_args else {}
                if not isinstance(args, dict):
                    args = {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            out = await registry.execute(name, args)
            if trace is not None:
                trace.append(
                    {
                        "name": name,
                        "args": args,
                        "ok": not out.startswith("ERROR:"),
                        "preview": out[:160],
                    }
                )
            work.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": out,
                }
            )
    # Model exhausted the rounds while still calling tools: force a final answer.
    work.append(
        {
            "role": "user",
            "content": "No more tool calls are available. Answer now with what you have.",
        }
    )
    resp = await llm.chat(role, work, task_key=task_key)
    return resp.message.get("content") or ""
