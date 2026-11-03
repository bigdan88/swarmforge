"""Tests for the live on_tool callback and new chat settings."""

from __future__ import annotations

import json

import pytest

from swarmforge.chat_tools import ToolRegistry, chat_with_tools
from swarmforge.config import ConfigError, parse_config

from .conftest import base_raw


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
    """Scripted LLM: pops canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, role, messages, *, tools=None, task_key=None, **kw):
        msg = self.responses.pop(0)
        return type("Resp", (), {"message": msg})()


def tool_call(name, args, call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


@pytest.mark.asyncio
async def test_on_tool_called_with_name_args_output():
    calls = []
    llm = FakeLLM(
        [
            {"tool_calls": [tool_call("echo", {"text": "hi"})]},
            {"content": "done"},
        ]
    )
    out = await chat_with_tools(
        llm, "dev", [{"role": "user", "content": "q"}], make_registry(),
        on_tool=lambda name, args, output: calls.append((name, args, output)),
    )
    assert out == "done"
    assert calls == [("echo", {"text": "hi"}, "echo:hi")]


@pytest.mark.asyncio
async def test_on_tool_not_called_without_tool_calls():
    calls = []
    llm = FakeLLM([{"content": "plain answer"}])
    out = await chat_with_tools(
        llm, "dev", [{"role": "user", "content": "q"}], make_registry(),
        on_tool=lambda *a: calls.append(a),
    )
    assert out == "plain answer"
    assert calls == []


@pytest.mark.asyncio
async def test_on_tool_raising_does_not_break_loop():
    def boom(*a):
        raise RuntimeError("callback broken")

    llm = FakeLLM(
        [
            {"tool_calls": [tool_call("echo", {"text": "x"})]},
            {"content": "still answered"},
        ]
    )
    out = await chat_with_tools(
        llm, "dev", [{"role": "user", "content": "q"}], make_registry(),
        on_tool=boom,
    )
    assert out == "still answered"


@pytest.mark.asyncio
async def test_on_tool_reports_multiple_calls_in_order():
    calls = []
    llm = FakeLLM(
        [
            {
                "tool_calls": [
                    tool_call("echo", {"text": "a"}, "c1"),
                    tool_call("echo", {"text": "b"}, "c2"),
                ]
            },
            {"content": "ok"},
        ]
    )
    out = await chat_with_tools(
        llm, "dev", [{"role": "user", "content": "q"}], make_registry(),
        on_tool=lambda name, args, output: calls.append((name, args)),
    )
    assert out == "ok"
    assert calls == [
        ("echo", {"text": "a"}),
        ("echo", {"text": "b"}),
    ]


def test_max_tool_rounds_zero_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"max_tool_rounds": 0}
    with pytest.raises(ConfigError, match="max_tool_rounds"):
        parse_config(raw)


def test_chat_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    cfg = parse_config(base_raw(tmp_path / "work"))
    assert cfg.chat.tools_enabled is True
    assert cfg.chat.max_tool_rounds == 5
