"""Offline tests for the chatbot's web tool wiring (fetch_url in the tool loop)."""

import pytest

from swarmforge.chatbot import SYSTEM_PROMPT, ask, build_chat_registry
from swarmforge.config import parse_config
from swarmforge.db import Store
from swarmforge.tools.web import WebToolError

from .conftest import FakeProvider, base_raw, tool_call


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    return parse_config(base_raw(tmp_path / "work"))


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "db.sqlite")


def _stub_fetch(monkeypatch, text="# Repo\n\nHello from the README"):
    calls = []

    async def fake_fetch_url(url, max_chars=8000, extract="markdown", **kw):
        calls.append({"url": url, "max_chars": max_chars, "extract": extract})
        return text

    monkeypatch.setattr("swarmforge.tools.web.fetch_url", fake_fetch_url)
    return calls


def _failing_fetch(monkeypatch):
    async def fake_fetch_url(url, max_chars=8000, extract="markdown", **kw):
        raise WebToolError("robots.txt disallows fetching this URL")

    monkeypatch.setattr("swarmforge.tools.web.fetch_url", fake_fetch_url)


def test_system_prompt_mentions_web_tool():
    assert "fetch_url" in SYSTEM_PROMPT
    assert "robots.txt" in SYSTEM_PROMPT


def test_build_chat_registry_exposes_fetch_url(cfg):
    registry = build_chat_registry(cfg)
    assert "fetch_url" in registry.names()
    schemas = registry.schemas()
    assert schemas and schemas[0]["function"]["name"] == "fetch_url"
    params = schemas[0]["function"]["parameters"]
    assert params["properties"]["url"]["type"] == "string"
    assert params["required"] == ["url"]


def test_answer_runs_tool_loop_and_returns_final_text(cfg, store, monkeypatch):
    calls = _stub_fetch(monkeypatch, "# Repo\n\nHello from the README")
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://github.com/o/r"})
                ],
            },
            {"role": "assistant", "content": "The repo says: Hello from the README"},
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    res = ask(
        cfg, store, "What is in https://github.com/o/r?", transport=fake.transport
    )
    assert res["answer"] == "The repo says: Hello from the README"
    assert calls == [
        {"url": "https://github.com/o/r", "max_chars": 8000, "extract": "markdown"}
    ]
    # Round 2 request contains the tool result message.
    msgs = fake.requests[1]["messages"]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "Hello from the README" in tool_msgs[0]["content"]
    # Round 1 request offered the tool schema.
    assert any(t["function"]["name"] == "fetch_url" for t in fake.requests[0]["tools"])


def test_answer_without_tool_calls_unchanged(cfg, store):
    fake = FakeProvider(
        {
            "qwen-fake": lambda body: {
                "role": "assistant",
                "content": "2 runs: 1 completed, 1 failed",
            }
        }
    )
    res = ask(cfg, store, "How many runs?", transport=fake.transport)
    assert res["answer"] == "2 runs: 1 completed, 1 failed"
    assert res["actions"] == {"tasks": [], "fixes": []}
    assert len(fake.requests) == 1


def test_tool_error_becomes_error_string_and_loop_completes(cfg, store, monkeypatch):
    _failing_fetch(monkeypatch)
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://example.com/x"})
                ],
            },
            {"role": "assistant", "content": "I could not fetch that page."},
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    res = ask(cfg, store, "Fetch https://example.com/x", transport=fake.transport)
    assert res["answer"] == "I could not fetch that page."
    tool_msgs = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs[0]["content"].startswith("ERROR: WebToolError")


def test_conversation_branch_uses_tools(cfg, store, monkeypatch):
    from swarmforge.chatbot import create_conversation

    conv = create_conversation(store)
    calls = _stub_fetch(monkeypatch, "# Doc\n\nDocs content")
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://docs.example.com/a"})
                ],
            },
            {
                "role": "assistant",
                "content": "Docs content, per https://docs.example.com/a",
            },
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    res = ask(
        cfg,
        store,
        "Summarize https://docs.example.com/a",
        conversation=conv["id"],
        transport=fake.transport,
    )
    assert res["answer"] == "Docs content, per https://docs.example.com/a"
    assert calls and calls[0]["url"] == "https://docs.example.com/a"
    assert res["conversation"] == conv["id"]


# ======================================================== tool gating + trace


def test_build_chat_registry_empty_tools_list(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"tools": []}
    cfg = parse_config(raw)
    assert build_chat_registry(cfg).names() == []


def test_build_chat_registry_unknown_tool_names_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"tools": ["nope"]}
    cfg = parse_config(raw)
    assert build_chat_registry(cfg).names() == []


def test_build_chat_registry_explicit_fetch_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"tools": ["fetch_url"]}
    cfg = parse_config(raw)
    assert build_chat_registry(cfg).names() == ["fetch_url"]


def test_build_chat_registry_default_registers_fetch_url(cfg):
    assert "fetch_url" in build_chat_registry(cfg).names()


def test_chat_with_tools_trace_success(cfg, monkeypatch):
    import asyncio

    from swarmforge.chat_tools import chat_with_tools

    _stub_fetch(monkeypatch, "# Repo\n\nHello from the README")
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://github.com/o/r"})
                ],
            },
            {"role": "assistant", "content": "done"},
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})

    class _LLM:
        async def chat(self, role, messages, tools=None, task_key="chatbot"):
            body = {"messages": messages, "tools": tools}
            msg = fake.handlers["qwen-fake"](body)
            return type("R", (), {"message": msg})()

    trace: list = []
    out = asyncio.run(
        chat_with_tools(
            _LLM(),
            "chat",
            [{"role": "user", "content": "q"}],
            build_chat_registry(cfg),
            trace=trace,
        )
    )
    assert out == "done"
    assert len(trace) == 1
    entry = trace[0]
    assert entry["name"] == "fetch_url"
    assert entry["args"] == {"url": "https://github.com/o/r"}
    assert entry["ok"] is True
    assert "Hello from the README" in entry["preview"]


def test_chat_with_tools_trace_error(cfg, store, monkeypatch):
    _failing_fetch(monkeypatch)
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://example.com/x"})
                ],
            },
            {"role": "assistant", "content": "I could not fetch that page."},
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    res = ask(cfg, store, "Fetch https://example.com/x", transport=fake.transport)
    assert res["answer"] == "I could not fetch that page."
    assert len(res["tools_used"]) == 1
    entry = res["tools_used"][0]
    assert entry["name"] == "fetch_url"
    assert entry["ok"] is False
    assert entry["preview"].startswith("ERROR:")


def test_ask_tools_used_empty_without_tool_calls(cfg, store):
    fake = FakeProvider(
        {
            "qwen-fake": lambda body: {
                "role": "assistant",
                "content": "2 runs: 1 completed, 1 failed",
            }
        }
    )
    res = ask(cfg, store, "How many runs?", transport=fake.transport)
    assert res["tools_used"] == []


def test_ask_tools_used_in_conversation_branch(cfg, store, monkeypatch):
    from swarmforge.chatbot import create_conversation

    conv = create_conversation(store)
    _stub_fetch(monkeypatch, "# Doc\n\nDocs content")
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://docs.example.com/a"})
                ],
            },
            {"role": "assistant", "content": "Docs content"},
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    res = ask(
        cfg,
        store,
        "Summarize https://docs.example.com/a",
        conversation=conv["id"],
        transport=fake.transport,
    )
    assert res["tools_used"] and res["tools_used"][0]["name"] == "fetch_url"
    assert res["tools_used"][0]["ok"] is True


def test_parse_config_chatbot_tools_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"tools": []}
    cfg = parse_config(raw)
    assert cfg.chatbot.tools == []
