"""Offline tests for the chatbot's sandbox tool wiring (chatbot.enable_shell)."""

import asyncio

from swarmforge.chatbot import SYSTEM_PROMPT, ask, build_chat_registry
from swarmforge.config import parse_config
from swarmforge.db import Store

from .conftest import FakeProvider, base_raw, tool_call


def _cfg(tmp_path, monkeypatch, enable_shell: bool):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    if enable_shell:
        raw["chatbot"] = {"enable_shell": True}
    return parse_config(raw)


def test_enable_shell_defaults_to_false(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, enable_shell=False)
    assert cfg.chatbot.enable_shell is False


def test_default_registry_has_only_fetch_url(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, enable_shell=False)
    assert set(build_chat_registry(cfg).names()) == {"fetch_url", "list_files", "repo_tree", "repo_file", "repo_search", "repo_issues"}


def test_enable_shell_registers_sandbox_tools(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, enable_shell=True)
    registry = build_chat_registry(cfg)
    names = registry.names()
    assert names[0] == "fetch_url"
    for name in ("run_command", "read_file", "grep"):
        assert name in names
    schemas = {s["function"]["name"]: s["function"] for s in registry.schemas()}
    assert schemas["run_command"]["parameters"]["required"] == ["command"]
    assert schemas["read_file"]["parameters"]["required"] == ["path"]
    assert schemas["grep"]["parameters"]["required"] == ["pattern"]
    assert schemas["run_command"]["parameters"]["properties"]["command"]["description"]
    assert schemas["read_file"]["parameters"]["properties"]["path"]["description"]
    assert schemas["grep"]["parameters"]["properties"]["pattern"]["description"]
    assert "read_file" in SYSTEM_PROMPT and "grep" in SYSTEM_PROMPT


def test_read_file_traversal_rejected_through_registry(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, enable_shell=True)
    registry = build_chat_registry(cfg)
    result = asyncio.run(registry.execute("read_file", {"path": "../evil"}))
    assert isinstance(result, str)
    assert result.startswith("ERROR:")


def test_run_command_echo(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, enable_shell=True)
    registry = build_chat_registry(cfg)
    result = asyncio.run(registry.execute("run_command", {"command": "echo hi"}))
    assert "hi" in result


def test_end_to_end_read_file_tool_loop(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("hello sandbox tools", encoding="utf-8")
    monkeypatch.chdir(project)
    cfg = _cfg(tmp_path, monkeypatch, enable_shell=True)
    store = Store(tmp_path / "db.sqlite")
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("read_file", {"path": "notes.txt"})
                ],
            },
            {"role": "assistant", "content": "The file says: hello sandbox tools"},
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    res = ask(cfg, store, "What is in notes.txt?", transport=fake.transport)
    assert res["answer"] == "The file says: hello sandbox tools"
    # The tool schema was offered in round 1 and the content reached the model in round 2.
    assert any(t["function"]["name"] == "read_file" for t in fake.requests[0]["tools"])
    tool_msgs = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "hello sandbox tools" in tool_msgs[0]["content"]


def test_tool_allowlist_cannot_grant_shell_access(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"enable_shell": False, "tools": ["read_file", "run_command"]}
    assert build_chat_registry(parse_config(raw)).names() == []
    raw["chatbot"] = {"enable_shell": True, "tools": ["read_file"]}
    assert build_chat_registry(parse_config(raw)).names() == ["read_file"]
    raw["chatbot"]["tools"] = []
    assert build_chat_registry(parse_config(raw)).names() == []
