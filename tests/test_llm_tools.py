"""Tool-calling loop: LLMClient.chat_with_tools in llm.py."""

import asyncio

import pytest

from swarmforge.llm import Budget, LLMClient, parse_tool_args

from .conftest import FakeProvider, tool_call


def _client(cfg, fake):
    return LLMClient(
        cfg, store=None, run_id=None, budget=Budget(5), transport=fake.transport
    )


def _run(coro):
    return asyncio.run(coro)


def test_parse_tool_args():
    assert parse_tool_args(None) == {}
    assert parse_tool_args("") == {}
    assert parse_tool_args('{"a": 1}') == {"a": 1}
    assert parse_tool_args('"{\\"a\\": 1}"') == {"a": 1}  # double-encoded
    with pytest.raises(ValueError):
        parse_tool_args("not json")


def test_tool_call_then_final_answer(cfg):
    calls = []

    def handler(body):
        calls.append(body)
        if len(calls) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://x.test"}, cid="c1")
                ],
            }
        return {"role": "assistant", "content": "final answer"}

    fake = FakeProvider({"qwen-fake": handler})
    client = _client(cfg, fake)

    def execute(name, args):
        assert name == "fetch_url"
        assert args == {"url": "https://x.test"}
        return "page content"

    messages = [{"role": "user", "content": "hi"}]
    res = _run(
        client.chat_with_tools(
            "planner",
            messages,
            [{"type": "function", "function": {"name": "fetch_url"}}],
            execute,
        )
    )

    assert res.content == "final answer"
    assert len(calls) == 2
    # messages mutated in place: assistant tool_calls message + tool message
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "fetch_url"
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "page content",
    }
    # second request body included them
    sent = calls[1]["messages"]
    assert sent[1]["role"] == "assistant" and sent[1]["tool_calls"]
    assert sent[2]["role"] == "tool" and sent[2]["tool_call_id"] == "c1"
    # tools were sent on both requests
    assert all("tools" in c for c in calls)


def test_max_rounds_exhaustion(cfg):

    def handler(body):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call("list_files", {"path": "."}, cid="c9")],
        }

    fake = FakeProvider({"qwen-fake": handler})
    client = _client(cfg, fake)
    messages = [{"role": "user", "content": "hi"}]
    res = _run(
        client.chat_with_tools("planner", messages, [], lambda n, a: "x", max_rounds=3)
    )
    assert len(fake.requests) == 3
    assert res.tool_calls  # last result still asks for tools
    # 3 assistant + 3 tool messages appended
    assert len(messages) == 7


def test_executor_error_becomes_error_tool_message(cfg):

    def handler(body):
        if len(fake.requests) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call("boom", {}, cid="c1")],
            }
        return {"role": "assistant", "content": "recovered"}

    fake = FakeProvider({"qwen-fake": handler})
    client = _client(cfg, fake)

    def execute(name, args):
        raise RuntimeError("kaboom")

    messages = [{"role": "user", "content": "hi"}]
    res = _run(client.chat_with_tools("planner", messages, [], execute))
    assert res.content == "recovered"
    tool_msg = messages[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"].startswith("ERROR:")
    assert "RuntimeError" in tool_msg["content"]


def test_on_tool_callback(cfg):

    def handler(body):
        if len(fake.requests) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    tool_call("a", {"x": 1}, cid="c1"),
                    tool_call("b", {"y": 2}, cid="c2"),
                ],
            }
        return {"role": "assistant", "content": "done"}

    fake = FakeProvider({"qwen-fake": handler})
    client = _client(cfg, fake)

    seen = []

    async def on_tool(name, args):
        seen.append((name, args))

    async def execute(name, args):
        seen.append(("exec", name))
        return "ok"

    messages = [{"role": "user", "content": "hi"}]
    res = _run(
        client.chat_with_tools("planner", messages, [], execute, on_tool=on_tool)
    )
    assert res.content == "done"
    assert seen == [("a", {"x": 1}), ("exec", "a"), ("b", {"y": 2}), ("exec", "b")]


def test_on_tool_errors_swallowed(cfg):

    def handler(body):
        if len(fake.requests) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call("a", {}, cid="c1")],
            }
        return {"role": "assistant", "content": "done"}

    fake = FakeProvider({"qwen-fake": handler})
    client = _client(cfg, fake)

    def on_tool(name, args):
        raise ValueError("callback boom")

    messages = [{"role": "user", "content": "hi"}]
    res = _run(
        client.chat_with_tools(
            "planner", messages, [], lambda n, a: "out", on_tool=on_tool
        )
    )
    assert res.content == "done"
    assert messages[2]["content"] == "out"
