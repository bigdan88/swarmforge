"""Acceptance tests for the chatbot tool-calling loop (issue: chat_with_tools + ToolRegistry).

Maps to acceptance criteria AC1-AC5. Everything runs offline: the LLM is the
FakeProvider (httpx.MockTransport) and fetch_url gets an injected mock transport.
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from swarmforge import chatbot
from swarmforge.chatbot import (
    ToolRegistry,
    ask,
    build_tools,
    fetch_url,
    list_files,
)
from swarmforge.config import parse_config
from swarmforge.db import Store
from swarmforge.llm import Budget, LLMClient

from .conftest import FakeProvider, base_raw, tool_call


def _run(coro):
    return asyncio.run(coro)


def _client(cfg, fake):
    return LLMClient(
        cfg, store=None, run_id=None, budget=Budget(5), transport=fake.transport
    )


# ------------------------------------------------------------------ AC1: loop
def test_ac1_tool_call_then_final_answer_second_request_has_both_messages(cfg):
    """Mock LLM returns a tool_call first, final text afterwards; the second
    request must contain the assistant tool_calls message and the matching
    tool-result message, and the final answer is returned."""

    def handler(body):
        if len(fake.requests) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://x.test"}, cid="c1")
                ],
            }
        return {"role": "assistant", "content": "final answer"}

    fake = FakeProvider({"qwen-fake": handler})
    messages = [{"role": "user", "content": "hi"}]
    res = _run(
        _client(cfg, fake).chat_with_tools(
            "planner",
            messages,
            [{"type": "function", "function": {"name": "fetch_url"}}],
            lambda name, args: "page content",
        )
    )
    assert res.content == "final answer"
    assert len(fake.requests) == 2
    sent = fake.requests[1]["messages"]
    assert sent[1] == {**messages[1]}  # assistant message echoed back
    assert sent[1]["role"] == "assistant"
    assert sent[1]["tool_calls"][0]["function"]["name"] == "fetch_url"
    assert sent[2] == {"role": "tool", "tool_call_id": "c1", "content": "page content"}
    # tool schemas are sent with every request of the loop
    assert all("tools" in r for r in fake.requests)


def test_ac1_max_rounds_exhausted_returns_last_result(cfg):
    """Loop stops after max_rounds model calls and returns the last result."""

    def handler(body):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call("list_files", {"path": "."}, cid="c9")],
        }

    fake = FakeProvider({"qwen-fake": handler})
    messages = [{"role": "user", "content": "hi"}]
    res = _run(
        _client(cfg, fake).chat_with_tools(
            "planner", messages, [], lambda n, a: "x", max_rounds=2
        )
    )
    assert len(fake.requests) == 2
    assert res.tool_calls  # the last (never answered) tool-call round is returned


def test_ac1_invalid_tool_arguments_become_error_tool_message(cfg):
    """A tool_call whose `arguments` is not valid JSON yields an ERROR tool
    message; on_tool does not fire for it and the loop continues."""

    def handler(body):
        if len(fake.requests) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "b1",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": "{oops"},
                    }
                ],
            }
        return {"role": "assistant", "content": "recovered"}

    fake = FakeProvider({"qwen-fake": handler})
    seen = []
    messages = [{"role": "user", "content": "hi"}]
    res = _run(
        _client(cfg, fake).chat_with_tools(
            "planner",
            messages,
            [],
            lambda n, a: pytest.fail("execute must not run for unparsable args"),
            on_tool=lambda n, a: seen.append(n),
        )
    )
    assert res.content == "recovered"
    assert seen == []
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "b1"
    assert messages[2]["content"].startswith(
        "ERROR: ValueError: invalid tool arguments"
    )


def test_ac1_multiple_tool_calls_each_get_matching_tool_message(cfg):
    """Two tool_calls in one assistant message -> two tool messages with the
    matching tool_call_ids, both fed back in the next request."""

    def handler(body):
        if len(fake.requests) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    tool_call("a", {"x": 1}, cid="id-1"),
                    tool_call("b", {"x": 2}, cid="id-2"),
                ],
            }
        return {"role": "assistant", "content": "done"}

    fake = FakeProvider({"qwen-fake": handler})
    messages = [{"role": "user", "content": "hi"}]
    res = _run(
        _client(cfg, fake).chat_with_tools(
            "planner", messages, [], lambda n, a: f"out:{n}"
        )
    )
    assert res.content == "done"
    assert [(m["tool_call_id"], m["content"]) for m in messages[2:4]] == [
        ("id-1", "out:a"),
        ("id-2", "out:b"),
    ]
    sent = fake.requests[1]["messages"]
    assert [m["tool_call_id"] for m in sent[2:4]] == ["id-1", "id-2"]


# ------------------------------------------------------------- AC2: registry
def test_ac2_registry_schemas_are_openai_style():
    reg = ToolRegistry()
    reg.register(
        "echo",
        lambda x: x,
        "Echo.",
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    )
    assert reg.schemas() == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo.",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            },
        }
    ]


def test_ac2_registry_execute_never_raises():
    reg = ToolRegistry()
    # unknown tool
    assert _run(reg.execute("nope", {})) == "ERROR: unknown tool 'nope'"
    # tool that raises
    reg.register("boom", lambda: 1 / 0, "Fails.", {"type": "object", "properties": {}})
    assert _run(reg.execute("boom", {})).startswith("ERROR: ZeroDivisionError:")
    # wrong kwargs (missing required arg / bad type) -> error string, no raise
    reg.register(
        "needs_url",
        fetch_url,
        "Fetch.",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    )
    assert _run(reg.execute("needs_url", {})).startswith("ERROR: TypeError:")
    reg.register(
        "list_files", list_files, "List.", {"type": "object", "properties": {}}
    )
    assert _run(reg.execute("list_files", {"path": 123})).startswith(
        "ERROR: TypeError:"
    )


def test_ac2_config_opt_in_fields():
    """chat.tools_enabled defaults to False; chat.max_tool_rounds defaults to 5;
    both are parsed from YAML config."""
    cfg = parse_config(base_raw(Path("work")))
    assert cfg.chat.tools_enabled is False
    assert cfg.chat.max_tool_rounds == 5
    raw = base_raw(Path("work"))
    raw["chat"] = {"tools_enabled": True, "max_tool_rounds": 3}
    cfg = parse_config(raw)
    assert cfg.chat.tools_enabled is True
    assert cfg.chat.max_tool_rounds == 3


# ------------------------------------------------------- AC3: builtin tools
def test_ac3_default_registry_has_both_builtins(cfg):
    reg = build_tools(cfg)
    assert reg.names() == ["fetch_url", "list_files"]
    names = {s["function"]["name"] for s in reg.schemas()}
    assert names == {"fetch_url", "list_files"}


def test_ac3_fetch_url_gets_follows_redirects_caps_size():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, text="B" * 500)

    out = fetch_url(
        "https://x.test/start", max_chars=50, transport=httpx.MockTransport(handler)
    )
    assert seen == ["/start", "/final"]  # redirect followed
    assert out.startswith("HTTP 200\n")
    assert len(out) <= 50 + len("HTTP 200\n") + len("\n…[truncated]…")
    assert "truncated" in out


def test_ac3_list_files_recursive_capped(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "sub").mkdir()
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x")
    (tmp_path / "sub" / "deep.txt").write_text("x")
    out = list_files(str(tmp_path))
    assert "f0.txt" in out and "sub/deep.txt" in out and "sub/" in out
    assert ".git" not in out
    capped = list_files(str(tmp_path), max_entries=2)
    assert len([l for l in capped.splitlines() if not l.startswith("…")]) == 2


def test_ac3_ask_end_to_end_fetch_url_through_registry(tmp_path, monkeypatch):
    """fetch_url is really executed end-to-end inside ask() when
    chat.tools_enabled is true (transport injected for offline testing)."""
    monkeypatch.setenv("FAKE_KEY", "test-key")
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"tools_enabled": True}
    cfg = parse_config(raw)
    store = Store(tmp_path / "db.sqlite")

    real_fetch = chatbot.fetch_url
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="HI FROM PAGE")
    )
    monkeypatch.setattr(
        chatbot,
        "fetch_url",
        lambda url, **kw: real_fetch(url, transport=transport, **kw),
    )

    def handler(body):
        if len(fake.requests) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://page.test/doc"}, cid="f1")
                ],
            }
        return {"role": "assistant", "content": "The page says HI FROM PAGE."}

    fake = FakeProvider({"qwen-fake": handler})
    res = ask(cfg, store, "what does the page say?", transport=fake.transport)
    assert res["answer"] == "The page says HI FROM PAGE."
    tool_msg = fake.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "f1"
    assert "HI FROM PAGE" in tool_msg["content"]


def test_ac3_answer_conversation_path_uses_tools(tmp_path, monkeypatch):
    """With a conversation id, tools run mid-answer and only the final text is
    persisted to the conversation (no tool chatter)."""
    monkeypatch.setenv("FAKE_KEY", "test-key")
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"tools_enabled": True}
    cfg = parse_config(raw)
    store = Store(tmp_path / "db.sqlite")
    (tmp_path / "note.txt").write_text("x")

    def handler(body):
        if len(fake.requests) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    tool_call("list_files", {"path": str(tmp_path)}, cid="t1")
                ],
            }
        return {"role": "assistant", "content": "I saw note.txt"}

    fake = FakeProvider({"qwen-fake": handler})
    calls = []
    res = ask(
        cfg,
        store,
        "files here?",
        conversation="acc-conv",
        transport=fake.transport,
        on_tool=lambda name, args: calls.append(name),
    )
    assert res["answer"] == "I saw note.txt"
    assert res["conversation"] == "acc-conv"
    assert calls == ["list_files"]
    msgs = store.conversation_messages("acc-conv")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "I saw note.txt"


def test_ac3_max_tool_rounds_from_config_is_wired(tmp_path, monkeypatch):
    """cfg.chat.max_tool_rounds limits the number of model calls in ask()."""
    monkeypatch.setenv("FAKE_KEY", "test-key")
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"tools_enabled": True, "max_tool_rounds": 2}
    cfg = parse_config(raw)
    store = Store(tmp_path / "db.sqlite")

    def handler(body):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call("list_files", {"path": str(tmp_path)}, cid="c")],
        }

    fake = FakeProvider({"qwen-fake": handler})
    ask(cfg, store, "loop forever", transport=fake.transport)
    assert len(fake.requests) == 2


def test_ac3_ask_without_tools_enabled_never_sends_tools(tmp_path, monkeypatch):
    """Tools are strictly opt-in: default config sends no `tools` field and
    runs exactly one model call."""
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = parse_config(base_raw(tmp_path / "work"))
    store = Store(tmp_path / "db.sqlite")

    def handler(body):
        assert "tools" not in body
        return {"role": "assistant", "content": "plain answer"}

    fake = FakeProvider({"qwen-fake": handler})
    res = ask(cfg, store, "hi", transport=fake.transport)
    assert res["answer"] == "plain answer"
    assert len(fake.requests) == 1


# ---------------------------------------------------------------- AC4: mock
def test_ac4_all_offline_no_real_network(monkeypatch):
    """Guard: nothing in this acceptance suite may open a real socket."""
    import socket

    def guard(*a, **k):
        raise AssertionError("real network access attempted")

    monkeypatch.setattr(socket.socket, "connect", guard)
    # fetch_url with a mock transport still works with sockets blocked
    out = fetch_url(
        "https://x.test/",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")),
    )
    assert out == "HTTP 200\nok"
    # and list_files works too
    assert "test_acceptance_chat_tools.py" in list_files(str(Path(__file__).parent))


def test_chat_with_tools_rejects_max_rounds_below_one(cfg):
    """Regression: max_rounds=0 used to skip the loop and crash on an assert;
    it must raise ValueError before any LLM call is made."""
    fake = FakeProvider({"qwen-fake": lambda body: {"role": "assistant", "content": "x"}})
    client = _client(cfg, fake)
    messages = [{"role": "user", "content": "hi"}]
    for bad in (0, -1):
        with pytest.raises(ValueError):
            _run(client.chat_with_tools("planner", messages, [], lambda n, a: "x", max_rounds=bad))
    assert fake.requests == []
    assert messages == [{"role": "user", "content": "hi"}]
