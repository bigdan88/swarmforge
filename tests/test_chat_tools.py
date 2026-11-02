"""Tests for swarmforge.chat_tools (ToolRegistry + chat_with_tools)."""

from __future__ import annotations

import asyncio
import json

import pytest

from swarmforge.chat_tools import ToolRegistry, chat_with_tools


def make_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(
        "echo",
        "Echo text back",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        lambda text="": f"echo:{text}",
    )
    return r


class FakeLLM:
    """Scripted LLM: pops canned responses; records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, role, messages, *, tools=None, task_key=None, **kw):
        self.requests.append(
            {
                "role": role,
                "messages": list(messages),
                "tools": tools,
                "task_key": task_key,
            }
        )
        msg = self.responses.pop(0)
        return type("Resp", (), {"message": msg})()


def tool_call(name, args, call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# --- ToolRegistry ---


@pytest.mark.asyncio
async def test_unknown_tool_error():
    r = make_registry()
    out = await r.execute("nope", {})
    assert out == "ERROR: unknown tool 'nope'. Available: echo"


@pytest.mark.asyncio
async def test_sync_handler():
    r = make_registry()
    assert await r.execute("echo", {"text": "hi"}) == "echo:hi"


@pytest.mark.asyncio
async def test_async_handler():
    r = ToolRegistry()

    async def slow(x: int = 0) -> str:
        await asyncio.sleep(0)
        return f"got {x}"

    r.register("slow", "Async tool", {"type": "object"}, slow)
    assert await r.execute("slow", {"x": 5}) == "got 5"


@pytest.mark.asyncio
async def test_handler_runtime_error():
    r = ToolRegistry()

    def boom():
        raise RuntimeError("kaboom")

    r.register("boom", "Fails", {"type": "object"}, boom)
    out = await r.execute("boom", {})
    assert out == "ERROR: RuntimeError: kaboom"


@pytest.mark.asyncio
async def test_handler_type_error_bad_arguments():
    r = ToolRegistry()

    def strict(a: int) -> str:
        return str(a)

    r.register("strict", "Strict", {"type": "object"}, strict)
    out = await r.execute("strict", {"b": 1})
    assert out.startswith("ERROR: bad arguments for strict:")


def test_schemas_shape():
    r = make_registry()
    schemas = r.schemas()
    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo text back",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            },
        }
    ]


def test_register_replaces():
    r = make_registry()
    r.register("echo", "New desc", {"type": "object"}, lambda: "new")
    assert r.names() == ["echo"]
    assert r.schemas()[0]["function"]["description"] == "New desc"


# --- chat_with_tools ---


@pytest.mark.asyncio
async def test_tool_round_then_answer():
    r = make_registry()
    llm = FakeLLM(
        [
            {"content": None, "tool_calls": [tool_call("echo", {"text": "hello"})]},
            {"content": "final answer"},
        ]
    )
    messages = [{"role": "user", "content": "say hi"}]
    out = await chat_with_tools(llm, "chat", messages, r)
    assert out == "final answer"
    # second request saw the tool result
    second = llm.requests[1]["messages"]
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert tool_msgs == [
        {"role": "tool", "tool_call_id": "call_1", "content": "echo:hello"}
    ]
    # caller's list untouched
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_no_mutation_of_caller_messages():
    r = make_registry()
    llm = FakeLLM(
        [
            {"content": None, "tool_calls": [tool_call("echo", {"text": "x"})]},
            {"content": "done"},
        ]
    )
    messages = [{"role": "user", "content": "q"}]
    snapshot = json.dumps(messages)
    await chat_with_tools(llm, "chat", messages, r)
    assert json.dumps(messages) == snapshot


@pytest.mark.asyncio
async def test_max_rounds_zero_raises():
    r = make_registry()
    llm = FakeLLM([])
    with pytest.raises(ValueError):
        await chat_with_tools(
            llm, "chat", [{"role": "user", "content": "q"}], r, max_rounds=0
        )


@pytest.mark.asyncio
async def test_exhaustion_final_no_tools_answer():
    r = make_registry()
    llm = FakeLLM(
        [
            {"content": None, "tool_calls": [tool_call("echo", {"text": "a"}, "c1")]},
            {"content": None, "tool_calls": [tool_call("echo", {"text": "b"}, "c2")]},
            {"content": "forced answer"},
        ]
    )
    out = await chat_with_tools(
        llm, "chat", [{"role": "user", "content": "q"}], r, max_rounds=2
    )
    assert out == "forced answer"
    final = llm.requests[-1]
    assert final["tools"] is None
    assert any(
        m.get("role") == "user" and "No more tool calls" in m.get("content", "")
        for m in final["messages"]
    )


@pytest.mark.asyncio
async def test_invalid_json_arguments_become_empty():
    r = make_registry()
    llm = FakeLLM(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "not json"},
                    }
                ],
            },
            {"content": "ok"},
        ]
    )
    out = await chat_with_tools(llm, "chat", [{"role": "user", "content": "q"}], r)
    assert out == "ok"
    tool_msg = next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == "echo:"  # default arg used
