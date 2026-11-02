"""Offline tests for the chatbot ToolRegistry, built-in tools and opt-in wiring."""

import asyncio

import httpx
import pytest

from swarmforge.chatbot import (
    ToolRegistry,
    ask,
    build_tools,
    fetch_url,
    list_files,
)
from swarmforge.config import parse_config
from swarmforge.db import Store

from .conftest import FakeProvider, base_raw, tool_call

# ------------------------------------------------------------- ToolRegistry


def test_registry_schemas_shape():
    reg = ToolRegistry()
    reg.register(
        "echo",
        lambda x: x,
        "Echo a string.",
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    )
    assert reg.names() == ["echo"]
    assert reg.has("echo") and not reg.has("nope")
    assert reg.schemas() == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo a string.",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            },
        }
    ]


def test_registry_execute_unknown_tool():
    reg = ToolRegistry()
    assert asyncio.run(reg.execute("missing", {})) == "ERROR: unknown tool 'missing'"


def test_registry_execute_raising_fn():
    reg = ToolRegistry()
    reg.register(
        "boom", lambda: 1 / 0, "Always fails.", {"type": "object", "properties": {}}
    )
    out = asyncio.run(reg.execute("boom", {}))
    assert out.startswith("ERROR: ZeroDivisionError:")


def test_registry_execute_async_fn():
    reg = ToolRegistry()

    async def slow(value: str = "x") -> str:
        return "async:" + value

    reg.register("slow", slow, "Async tool.", {"type": "object", "properties": {}})
    assert asyncio.run(reg.execute("slow", {"value": "y"})) == "async:y"


# ------------------------------------------------------------- fetch_url


def test_fetch_url_truncates_and_follows_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redir":
            return httpx.Response(302, headers={"Location": "https://x.test/page"})
        return httpx.Response(200, text="A" * 100)

    out = fetch_url(
        "https://x.test/redir", max_chars=10, transport=httpx.MockTransport(handler)
    )
    assert out.startswith("HTTP 200\n")
    assert "…[truncated]…" in out
    assert len(out.split("\n", 1)[1]) <= 10 + len("\n…[truncated]…")


def test_fetch_url_transport_error_becomes_error_string():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    reg = ToolRegistry()
    reg.register(
        "fetch_url",
        lambda url: fetch_url(url, transport=httpx.MockTransport(handler)),
        "fetch",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    )
    out = asyncio.run(reg.execute("fetch_url", {"url": "https://x.test/"}))
    assert out.startswith("ERROR: ConnectError")


# ------------------------------------------------------------- list_files


def test_list_files_sorted_skips_git_and_caps(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "b.py").write_text("x")
    sub = tmp_path / "a"
    sub.mkdir()
    (sub / "c.txt").write_text("x")
    out = list_files(str(tmp_path))
    assert out == "a/\na/c.txt\nb.py"
    assert ".git" not in out and "__pycache__" not in out

    capped = list_files(str(tmp_path), max_entries=1)
    assert capped.startswith("a/")
    assert "truncated" in capped


def test_list_files_missing_dir_raises():
    reg = ToolRegistry()
    reg.register("list_files", list_files, "list", {"type": "object", "properties": {}})
    out = asyncio.run(reg.execute("list_files", {"path": "/no/such/dir/xyz"}))
    assert out.startswith("ERROR: FileNotFoundError")


def test_list_files_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(NotADirectoryError):
        list_files(str(f))


# ------------------------------------------------------------- build_tools


def test_build_tools_registers_builtins(cfg):
    reg = build_tools(cfg)
    assert reg.names() == ["fetch_url", "list_files"]
    schemas = {s["function"]["name"]: s for s in reg.schemas()}
    assert schemas["fetch_url"]["function"]["parameters"]["required"] == ["url"]
    assert schemas["list_files"]["function"]["parameters"]["required"] == []
    for s in reg.schemas():
        assert s["type"] == "function"


# ------------------------------------------------------------- end-to-end wiring


def _seeded_store(tmp_path) -> Store:
    return Store(tmp_path / "db.sqlite")


def test_ask_with_tools_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"tools_enabled": True}
    cfg = parse_config(raw)
    store = _seeded_store(tmp_path)

    calls: list[tuple[str, dict]] = []
    seen: list[int] = []

    def handler(body: dict) -> dict:
        seen.append(1)
        if len(seen) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call("list_files", {"path": str(tmp_path)})],
            }
        return {"role": "assistant", "content": "All done: saw the files."}

    fake = FakeProvider({"qwen-fake": handler})
    res = ask(
        cfg,
        store,
        "list the files",
        transport=fake.transport,
        on_tool=lambda name, args: calls.append((name, args)),
    )
    assert res["answer"] == "All done: saw the files."
    assert calls == [("list_files", {"path": str(tmp_path)})]
    assert len(fake.requests) == 2
    assert "tools" in fake.requests[0]
    second = fake.requests[1]
    msgs = second["messages"]
    assert msgs[-2]["role"] == "assistant"
    assert msgs[-2]["tool_calls"][0]["function"]["name"] == "list_files"
    assert msgs[-1]["role"] == "tool"
    assert "file.txt" in msgs[-1]["content"] or msgs[-1]["content"]


def test_tools_opt_in_default_off(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = parse_config(base_raw(tmp_path / "work"))
    store = _seeded_store(tmp_path)

    def handler(body: dict) -> dict:
        assert "tools" not in body
        return {"role": "assistant", "content": "plain answer"}

    fake = FakeProvider({"qwen-fake": handler})
    res = ask(cfg, store, "hi", transport=fake.transport)
    assert res["answer"] == "plain answer"
    assert len(fake.requests) == 1
    assert "tools" not in fake.requests[0]


def test_config_defaults_and_parse(tmp_path):
    cfg = parse_config(base_raw(tmp_path / "work"))
    assert cfg.chat.tools_enabled is False
    assert cfg.chat.max_tool_rounds == 5
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"tools_enabled": True, "max_tool_rounds": 3}
    cfg = parse_config(raw)
    assert cfg.chat.tools_enabled is True
    assert cfg.chat.max_tool_rounds == 3
