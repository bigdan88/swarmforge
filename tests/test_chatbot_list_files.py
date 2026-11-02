"""Offline tests for the chatbot's list_files tool and chat settings wiring."""

import pytest

from swarmforge.chatbot import ask, build_chat_registry, list_files
from swarmforge.config import parse_config
from swarmforge.db import Store

from .conftest import FakeProvider, base_raw, tool_call


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "db.sqlite")


def _make_tree(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "README.md").write_text("# proj\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("")
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "app.cpython-312.pyc").write_text("")
    (root / ".pytest_cache").mkdir()
    (root / ".pytest_cache" / "v").write_text("")
    return root


def test_registry_names(cfg):
    assert build_chat_registry(cfg).names()[:2] == ["fetch_url", "list_files"]


def test_list_files_sorted_dirs_slash_and_skips(tmp_path):
    root = _make_tree(tmp_path)
    out = list_files(root)
    assert out == "README.md\nsrc/\nsrc/app.py"


def test_list_files_truncation(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(10):
        (root / f"f{i}.txt").write_text("x")
    out = list_files(root, max_entries=4)
    lines = out.splitlines()
    assert len(lines) == 5
    assert lines[:4] == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]
    assert lines[4] == "… (6 more)"


def test_list_files_empty_dir(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert list_files(root) == ""


def test_list_files_errors(tmp_path):
    import asyncio

    registry = build_chat_registry(cfg_factory(tmp_path))
    out = asyncio.run(
        registry.execute("list_files", {"path": str(tmp_path / "missing")})
    )
    assert isinstance(out, str)
    assert out.startswith("ERROR:")
    f = tmp_path / "file.txt"
    f.write_text("x")
    out = asyncio.run(registry.execute("list_files", {"path": str(f)}))
    assert out.startswith("ERROR:")


def cfg_factory(tmp_path, **chat_over):
    import os

    os.environ.setdefault("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"context_max_messages": 12, "context_max_tokens": 4000, **chat_over}
    return parse_config(raw)


def test_ask_list_files_conversation_and_on_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    root = _make_tree(tmp_path)
    cfg = cfg_factory(tmp_path)
    store = Store(tmp_path / "db.sqlite")
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call("list_files", {"path": str(root)})],
            },
            {
                "role": "assistant",
                "content": "The project has a README and src/app.py.",
            },
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    seen = []
    res = ask(
        cfg,
        store,
        "What files are in the project?",
        transport=fake.transport,
        on_tool=lambda name, args, out: seen.append((name, args, out)),
    )
    assert res["answer"] == "The project has a README and src/app.py."
    assert len(seen) == 1
    name, args, out = seen[0]
    assert name == "list_files"
    assert args == {"path": str(root)}
    assert "src/app.py" in out
    msgs = fake.requests[1]["messages"]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "src/app.py" in tool_msgs[0]["content"]


def test_tools_disabled_sends_no_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    cfg = cfg_factory(tmp_path, tools_enabled=False)
    store = Store(tmp_path / "db.sqlite")
    fake = FakeProvider(
        {"qwen-fake": lambda body: {"role": "assistant", "content": "No tools here."}}
    )
    res = ask(cfg, store, "Hi", transport=fake.transport)
    assert res["answer"] == "No tools here."
    assert len(fake.requests) == 1
    assert "tools" not in fake.requests[0]


def test_max_tool_rounds_one_forces_final_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    cfg = cfg_factory(tmp_path, max_tool_rounds=1)
    store = Store(tmp_path / "db.sqlite")
    root = _make_tree(tmp_path)

    def handler(body):
        if body.get("tools"):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call("list_files", {"path": str(root)})],
            }
        return {"role": "assistant", "content": "Forced final answer."}

    fake = FakeProvider({"qwen-fake": handler})
    res = ask(cfg, store, "List the files", transport=fake.transport)
    assert res["answer"] == "Forced final answer."
    assert len(fake.requests) == 2
    assert "tools" not in fake.requests[1]
