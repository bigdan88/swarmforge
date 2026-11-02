"""Acceptance tests for the chatbot tool extension.

Covers, one test (or small group) per criterion of the issue:

  AC1 exactly one ToolRegistry class and one tool loop in src/ (no second registry,
      no second loop, no second fetch_url)
  AC2 build_chat_registry(cfg).names() == ["fetch_url", "list_files"]
  AC3 list_files: sorted relative paths, dirs end with '/', skips .git/__pycache__/
      .pytest_cache, truncates with '… (N more)', raises for a missing path or a
      non-directory (the registry turns that into an 'ERROR:' string)
  AC4 chat.tools_enabled: false -> the model request contains no tools at all
  AC5 a conversation where the model calls list_files then answers returns the answer
      and on_tool receives (name, args, output)
  AC6 ask()/answer() pass max_rounds=cfg.chat.max_tool_rounds; max_tool_rounds: 0
      raises ConfigError; defaults; both settings documented in swarmforge.example.yaml
  AC7 on the Chat page a tool call appears as a caption inside the assistant's reply
  AC8 (existing chat_tools / web-fetch tests) are exercised by their own files
  AC9/AC10 workspace git retry helper (see also tests/test_workspace_git_retry.py)

Everything runs offline: FakeProvider / httpx.MockTransport only.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from swarmforge import chatbot
from swarmforge.chat_tools import ToolRegistry, chat_with_tools
from swarmforge.chatbot import build_chat_registry, list_files
from swarmforge.config import ConfigError, parse_config
from swarmforge.db import Store
from swarmforge.workspace import _TRANSIENT_GIT_ERRORS

from .conftest import FakeProvider, base_raw, tool_call

SRC = Path(__file__).resolve().parents[1] / "src"
EXAMPLE_YAML = Path(__file__).resolve().parents[1] / "swarmforge.example.yaml"

ASSISTANT_AVATAR = ":material/auto_awesome:"


# ---------------------------------------------------------------- fixtures / helpers
@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")


def make_cfg(tmp_path: Path, workdir_name: str = "work", **chat: Any):
    """Config whose workdir is `tmp_path/<workdir_name>`, with chat: overrides merged in."""
    raw = base_raw(tmp_path / workdir_name)
    raw["chat"] = {**raw.get("chat", {}), **chat}
    return parse_config(raw)


def tree(root: Path) -> Path:
    """A small directory tree with the skip-dirs present at the root and nested."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "alpha.txt").write_text("a", encoding="utf-8")
    (root / "zeta.md").write_text("z", encoding="utf-8")
    pkg = root / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "beta.py").write_text("b", encoding="utf-8")
    (pkg / "sub" / "deep.txt").write_text("d", encoding="utf-8")
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("x", encoding="utf-8")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "beta.cpython-312.pyc").write_text("x", encoding="utf-8")
    (root / ".pytest_cache" / "v").mkdir(parents=True)
    (root / ".pytest_cache" / "v" / "cache").write_text("x", encoding="utf-8")
    return root


def assistant_bubbles(at):
    return [m for m in at.chat_message if m.avatar == ASSISTANT_AVATAR]


# ============================================================================ AC1
def test_ac1_exactly_one_tool_registry_and_one_tool_loop_in_src():
    """grep-style check: one ToolRegistry, one tool loop, one fetch_url implementation."""
    registries, loops, fetches, builders = [], [], [], []
    for py in sorted(SRC.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        rel = str(py.relative_to(SRC))
        if re.search(r"^\s*class ToolRegistry\b", text, re.MULTILINE):
            registries.append(rel)
        if re.search(r"^\s*(?:async )?def chat_with_tools\b", text, re.MULTILINE):
            loops.append(rel)
        if re.search(r"^\s*(?:async )?def fetch_url\b", text, re.MULTILINE):
            fetches.append(rel)
        if re.search(r"^\s*(?:async )?def build_chat_registry\b", text, re.MULTILINE):
            builders.append(rel)
    assert registries == ["swarmforge/chat_tools.py"], registries
    assert loops == ["swarmforge/chat_tools.py"], loops
    assert fetches == ["swarmforge/tools/web.py"], fetches
    assert builders == ["swarmforge/chatbot.py"], builders


def test_ac1_chatbot_reuses_the_shared_registry_and_loop(key, tmp_path):
    """build_chat_registry returns the *same* ToolRegistry class and chatbot.ask runs
    chat_with_tools from chat_tools (no parallel implementation was added)."""
    cfg = make_cfg(tmp_path)
    registry = build_chat_registry(cfg)
    assert type(registry) is ToolRegistry
    assert sys.modules["swarmforge.chatbot"].chat_with_tools is chat_with_tools
    assert chatbot.ToolRegistry is ToolRegistry


# ============================================================================ AC2
def test_ac2_build_chat_registry_registers_fetch_url_then_list_files(key, tmp_path):
    registry = build_chat_registry(make_cfg(tmp_path))
    assert registry.names() == ["fetch_url", "list_files"]


def test_ac2_list_files_schema_is_offered_to_the_model(key, tmp_path):
    schemas = build_chat_registry(make_cfg(tmp_path)).schemas()
    by_name = {s["function"]["name"]: s["function"] for s in schemas}
    assert set(by_name) == {"fetch_url", "list_files"}
    params = by_name["list_files"]["parameters"]["properties"]
    assert {"path", "max_entries"} <= set(params)
    assert params["path"]["type"] == "string"
    assert params["max_entries"]["type"] == "integer"
    assert by_name["fetch_url"]["description"]  # web tool still described as before


# ============================================================================ AC3
def test_ac3_sorted_relative_paths_with_directories_ending_in_slash(key, tmp_path):
    root = tree(tmp_path / "work")
    out = list_files(path=str(root))
    lines = out.splitlines()
    assert lines == sorted(lines)
    assert "pkg/" in lines and "pkg/sub/" in lines
    assert {"alpha.txt", "zeta.md", "pkg/beta.py", "pkg/sub/deep.txt"} <= set(lines)
    # relative paths only: never an absolute path, never the root itself
    assert not any(line.startswith(("/", str(root))) for line in lines)


def test_ac3_skips_git_pycache_and_pytest_cache(key, tmp_path):
    root = tree(tmp_path / "work")
    out = list_files(path=str(root))
    for skipped in (".git", "__pycache__", ".pytest_cache"):
        assert skipped not in out, (skipped, out)
    assert "HEAD" not in out and "beta.cpython-312.pyc" not in out


def test_ac3_truncates_with_a_more_line(key, tmp_path):
    root = tmp_path / "work"
    root.mkdir(parents=True)
    for i in range(7):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")
    out = list_files(path=str(root), max_entries=3)
    lines = out.splitlines()
    assert lines[:3] == ["f0.txt", "f1.txt", "f2.txt"]
    assert lines[3] == "… (4 more)"
    assert len(lines) == 4


def test_ac3_no_truncation_line_when_within_the_limit(key, tmp_path):
    root = tmp_path / "work"
    root.mkdir(parents=True)
    (root / "only.txt").write_text("x", encoding="utf-8")
    assert "more)" not in list_files(path=str(root))


def test_ac3_raises_for_missing_path_and_non_directory(key, tmp_path):
    root = tree(tmp_path / "work")
    with pytest.raises(FileNotFoundError):
        list_files(path=str(root / "nope"))
    with pytest.raises(NotADirectoryError):
        list_files(path=str(root / "alpha.txt"))


def test_ac3_registry_turns_list_files_errors_into_error_strings(key, tmp_path):
    tree(tmp_path / "work")
    registry = build_chat_registry(make_cfg(tmp_path))
    for args in ({"path": "does-not-exist"}, {"path": "alpha.txt"}):
        out = asyncio.run(registry.execute("list_files", args))
        assert out.startswith("ERROR:"), out


def test_ac3_defaults_path_and_max_entries(key, tmp_path, monkeypatch):
    root = tree(tmp_path / "work")
    monkeypatch.chdir(root)
    out = asyncio.run(build_chat_registry(make_cfg(tmp_path)).execute("list_files", {}))
    lines = out.splitlines()
    assert lines and lines[0] == "alpha.txt"
    assert ".git" not in out and "__pycache__" not in out


# ============================================================================ AC4
def test_ac4_tools_disabled_sends_no_tools_in_the_request(key, tmp_path):
    fake = FakeProvider(
        {"qwen-fake": lambda body: {"role": "assistant", "content": "no tools here"}}
    )
    cfg = make_cfg(tmp_path, tools_enabled=False)
    res = chatbot.ask(
        cfg, Store(tmp_path / "db.sqlite"), "hi", transport=fake.transport
    )
    assert res["answer"] == "no tools here"
    assert len(fake.requests) == 1
    assert "tools" not in fake.requests[0]


def test_ac4_tools_disabled_skips_the_tool_loop_entirely(key, tmp_path, monkeypatch):
    """answer() must not even enter chat_with_tools when tools are off."""
    seen: list[dict] = []

    async def spy(llm, role, messages, registry, **kw):
        seen.append(kw)
        return "from the loop"

    monkeypatch.setattr(chatbot, "chat_with_tools", spy)
    fake = FakeProvider(
        {"qwen-fake": lambda body: {"role": "assistant", "content": "plain answer"}}
    )
    cfg = make_cfg(tmp_path, tools_enabled=False)
    store = Store(tmp_path / "db.sqlite")
    out = asyncio.run(chatbot.answer(cfg, store, "hi", transport=fake.transport))
    assert out["answer"] == "plain answer"
    assert seen == []
    assert "tools" not in fake.requests[0]
    assert len(fake.requests) == 1


def test_ac4_tools_enabled_by_default_sends_the_two_tools(key, tmp_path):
    fake = FakeProvider(
        {"qwen-fake": lambda body: {"role": "assistant", "content": "answer"}}
    )
    cfg = make_cfg(tmp_path)
    chatbot.ask(cfg, Store(tmp_path / "db.sqlite"), "hi", transport=fake.transport)
    offered = [t["function"]["name"] for t in fake.requests[0]["tools"]]
    assert offered == ["fetch_url", "list_files"]


def test_ac4_conversation_branch_also_sends_no_tools(key, tmp_path):
    fake = FakeProvider(
        {"qwen-fake": lambda body: {"role": "assistant", "content": "answer"}}
    )
    cfg = make_cfg(tmp_path, tools_enabled=False)
    store = Store(tmp_path / "db.sqlite")
    conv = chatbot.create_conversation(store, "tools off")
    asyncio.run(
        chatbot.answer(
            cfg, store, "hi", conversation=conv["id"], transport=fake.transport
        )
    )
    assert len(fake.requests) == 1 and "tools" not in fake.requests[0]


# ============================================================================ AC5
def test_ac5_model_calls_list_files_then_answers(key, tmp_path, monkeypatch):
    root = tree(tmp_path / "work")
    (root / "readme.md").write_text("hello", encoding="utf-8")
    monkeypatch.chdir(root)

    def handler(body):
        if not any(m.get("role") == "tool" for m in body["messages"]):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call("list_files", {"path": "."})],
            }
        return {
            "role": "assistant",
            "content": "There is a readme.md and a pkg directory.",
        }

    fake = FakeProvider({"qwen-fake": handler})
    cfg = make_cfg(tmp_path)
    calls: list[tuple] = []
    res = chatbot.ask(
        cfg,
        Store(tmp_path / "db.sqlite"),
        "what files are here?",
        on_tool=lambda *a: calls.append(a),
        transport=fake.transport,
    )

    assert res["answer"] == "There is a readme.md and a pkg directory."
    # the tool really ran: the model sees its output in a tool message
    assert len(fake.requests) == 2
    tool_msgs = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "readme.md" in tool_msgs[0]["content"] and "pkg/" in tool_msgs[0]["content"]
    # on_tool got (name, args, output) for the call
    assert len(calls) == 1
    name, args, output = calls[0]
    assert name == "list_files" and args == {"path": "."}
    assert "readme.md" in output and "pkg/" in output


def test_ac5_answer_async_path_reports_the_tool_call(key, tmp_path, monkeypatch):
    root = tree(tmp_path / "work")
    monkeypatch.chdir(root)

    def handler(body):
        if not any(m.get("role") == "tool" for m in body["messages"]):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call("list_files", {})],
            }
        return {"role": "assistant", "content": "done"}

    fake = FakeProvider({"qwen-fake": handler})
    cfg = make_cfg(tmp_path)
    calls: list[tuple] = []
    out = asyncio.run(
        chatbot.answer(
            cfg,
            Store(tmp_path / "db.sqlite"),
            "list the files",
            transport=fake.transport,
            on_tool=lambda *a: calls.append(a),
        )
    )
    assert out["answer"] == "done"
    assert [c[0] for c in calls] == ["list_files"]
    assert "alpha.txt" in calls[0][2]


def test_ac5_tool_error_reaches_callback_and_model_without_raising(key, tmp_path):
    tree(tmp_path / "work")

    def handler(body):
        if not any(m.get("role") == "tool" for m in body["messages"]):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call("list_files", {"path": "nope"})],
            }
        return {"role": "assistant", "content": "sorry"}

    fake = FakeProvider({"qwen-fake": handler})
    cfg = make_cfg(tmp_path)
    calls: list[tuple] = []
    res = chatbot.ask(
        cfg,
        Store(tmp_path / "db.sqlite"),
        "list nope",
        on_tool=lambda *a: calls.append(a),
        transport=fake.transport,
    )
    assert res["answer"] == "sorry"
    assert calls[0][2].startswith("ERROR:")
    tool_msgs = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs[0]["content"].startswith("ERROR:")


def test_ac5_on_tool_is_optional_and_gets_nothing_without_calls(key, tmp_path):
    fake = FakeProvider(
        {"qwen-fake": lambda body: {"role": "assistant", "content": "just an answer"}}
    )
    cfg = make_cfg(tmp_path)
    res = chatbot.ask(
        cfg, Store(tmp_path / "db.sqlite"), "hi", transport=fake.transport
    )
    assert res["answer"] == "just an answer"


def test_ac5_on_tool_fires_after_each_tool_in_call_order():
    """The single tool loop calls on_tool(name, args, output) after every tool runs."""
    registry = ToolRegistry()
    registry.register(
        "echo",
        "echo back",
        {"type": "object", "properties": {}},
        lambda text="": f"out:{text}",
    )
    events: list[tuple] = []

    class FakeLLM:
        def __init__(self) -> None:
            self.rounds = 0

        async def chat(self, role, messages, **kw):
            self.rounds += 1
            if not any(m.get("role") == "tool" for m in messages):
                return SimpleNamespace(
                    message={
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "echo",
                                    "arguments": '{"text": "a"}',
                                },
                            },
                            {
                                "id": "c2",
                                "function": {
                                    "name": "echo",
                                    "arguments": '{"text": "b"}',
                                },
                            },
                        ]
                    }
                )
            return SimpleNamespace(message={"content": "final"})

    llm = FakeLLM()
    out = asyncio.run(
        chat_with_tools(
            llm,
            "planner",
            [{"role": "user", "content": "go"}],
            registry,
            on_tool=lambda n, a, o: events.append((n, a, o)),
        )
    )
    assert out == "final"
    assert events == [
        ("echo", {"text": "a"}, "out:a"),
        ("echo", {"text": "b"}, "out:b"),
    ]
    assert llm.rounds == 2


def test_ac5_a_broken_on_tool_callback_does_not_break_the_loop():
    registry = ToolRegistry()
    registry.register(
        "echo",
        "echo back",
        {"type": "object", "properties": {}},
        lambda text="": f"out:{text}",
    )

    class FakeLLM:
        async def chat(self, role, messages, **kw):
            if not any(m.get("role") == "tool" for m in messages):
                return SimpleNamespace(
                    message={
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "echo",
                                    "arguments": '{"text": "a"}',
                                },
                            }
                        ]
                    }
                )
            return SimpleNamespace(message={"content": "final"})

    def explode(name, args, output):
        raise RuntimeError("widget gone missing")

    assert (
        asyncio.run(
            chat_with_tools(
                FakeLLM(),
                "planner",
                [{"role": "user", "content": "go"}],
                registry,
                on_tool=explode,
            )
        )
        == "final"
    )


# ============================================================================ AC6
def test_ac6_ask_and_answer_pass_max_rounds_from_config(key, tmp_path, monkeypatch):
    seen: list[dict] = []

    async def spy(llm, role, messages, registry, **kw):
        seen.append(
            {
                "max_rounds": kw.get("max_rounds"),
                "on_tool": kw.get("on_tool"),
                "names": registry.names(),
            }
        )
        return "spied"

    monkeypatch.setattr(chatbot, "chat_with_tools", spy)
    cfg = make_cfg(tmp_path, max_tool_rounds=3)
    store = Store(tmp_path / "db.sqlite")
    res = chatbot.ask(cfg, store, "hi", on_tool=lambda *a: None)
    assert res["answer"] == "spied"
    assert seen[0]["max_rounds"] == 3
    assert callable(seen[0]["on_tool"]) and seen[0]["names"] == [
        "fetch_url",
        "list_files",
    ]

    seen.clear()
    out = asyncio.run(chatbot.answer(cfg, store, "hi", on_tool=lambda *a: None))
    assert out["answer"] == "spied" and seen[0]["max_rounds"] == 3


def test_ac6_default_max_rounds_is_five(key, tmp_path, monkeypatch):
    seen: list[dict] = []

    async def spy(llm, role, messages, registry, **kw):
        seen.append(kw)
        return "spied"

    monkeypatch.setattr(chatbot, "chat_with_tools", spy)
    chatbot.ask(make_cfg(tmp_path), Store(tmp_path / "db.sqlite"), "hi")
    assert seen[0]["max_rounds"] == 5


def test_ac6_max_tool_rounds_zero_raises_config_error(key, tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"max_tool_rounds": 0}
    with pytest.raises(ConfigError):
        parse_config(raw)


@pytest.mark.parametrize("bad", [-1, 0])
def test_ac6_max_tool_rounds_below_one_raises(key, tmp_path, bad):
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"max_tool_rounds": bad}
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_ac6_defaults_and_one_is_accepted(key, tmp_path):
    cfg = parse_config(base_raw(tmp_path / "work"))
    assert cfg.chat.tools_enabled is True
    assert cfg.chat.max_tool_rounds == 5
    raw = base_raw(tmp_path / "work")
    raw["chat"] = {"tools_enabled": False, "max_tool_rounds": 1}
    cfg = parse_config(raw)
    assert (cfg.chat.tools_enabled, cfg.chat.max_tool_rounds) == (False, 1)


def test_ac6_example_yaml_documents_both_settings():
    text = EXAMPLE_YAML.read_text(encoding="utf-8")
    section = re.search(r"^chat:.*\n(?:[ \t]+.*\n|\n)*", text, re.MULTILINE)
    assert section, "no top-level chat: section in swarmforge.example.yaml"
    block = section.group(0)
    loaded = yaml.safe_load(block)
    assert loaded["chat"]["tools_enabled"] is True
    assert loaded["chat"]["max_tool_rounds"] == 5
    for name in ("tools_enabled", "max_tool_rounds"):
        line = next(
            (ln for ln in block.splitlines() if ln.strip().startswith(name + ":")), ""
        )
        assert "#" in line, f"{name} is not documented in swarmforge.example.yaml"
        assert len(line.split("#", 1)[1].strip()) > 15, line


# ============================================================================ AC7
@pytest.fixture
def app_test(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from .test_dashboard import APP, _go, _write_cfg

    def build(fake_ask):
        monkeypatch.setattr(chatbot, "ask", fake_ask)
        _write_cfg(tmp_path, monkeypatch)
        at = AppTest.from_file(str(APP), default_timeout=60)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        _go(at, "Chat")
        assert not at.exception, [e.value for e in at.exception]
        return at

    return build


def _fake_ask(emit_tools):
    asked: list[dict] = []

    def fake(
        cfg, store, question, *, history=None, conversation=None, on_tool=None, **kw
    ):
        asked.append(
            {"question": question, "conversation": conversation, "on_tool": on_tool}
        )
        if on_tool:
            for name, args in emit_tools:
                on_tool(name, args, "tool output")
        return {
            "answer": "**fake answer**",
            "actions": {},
            "role": "planner",
            "model": "qwen-fake",
            "usage": {"cost_usd": 0.0},
            "conversation": conversation or "c1",
        }

    fake.asked = asked  # type: ignore[attr-defined]
    return fake


def test_ac7_tool_calls_appear_as_captions_in_the_assistant_bubble(app_test):
    fake = _fake_ask(
        [
            ("fetch_url", {"url": "https://example.com/page"}),
            ("list_files", {"path": "src"}),
        ]
    )
    at = app_test(fake)
    at.chat_input[0].set_value("what is on disk?").run()
    assert not at.exception, [e.value for e in at.exception]

    bubbles = assistant_bubbles(at)
    assert len(bubbles) == 1, bubbles
    bubble = bubbles[0]
    captions = [c.value for c in bubble.caption]
    assert "🔧 fetch_url · https://example.com/page" in captions
    assert "🔧 list_files · src" in captions
    # the answer is in the same bubble, and the tool captions were written while it
    # was still being prepared (they precede the 'answered by' footer caption)
    assert any("**fake answer**" in m.value for m in bubble.markdown)
    assert captions.index("🔧 fetch_url · https://example.com/page") < len(captions) - 1
    assert captions[-1].startswith("answered by")
    # chatbot.ask() was handed a live-tool callback
    assert fake.asked[0]["on_tool"] is not None


def test_ac7_no_caption_without_tool_calls(app_test):
    at = app_test(_fake_ask([]))
    at.chat_input[0].set_value("plain question").run()
    assert not at.exception, [e.value for e in at.exception]
    bubble = assistant_bubbles(at)[-1]
    assert not any("🔧" in c.value for c in bubble.caption), [
        c.value for c in bubble.caption
    ]
    assert any("**fake answer**" in m.value for m in bubble.markdown)


# ==================================================================== AC9 / AC10
def test_ac9_all_git_calls_go_through_the_retry_helper():
    """No git subprocess is started anywhere in workspace.py except the retry helper,
    and both public entry points (git / git_rc) route through it."""
    text = (SRC / "swarmforge" / "workspace.py").read_text(encoding="utf-8")
    launches = re.findall(
        r"subprocess\.(?:run|Popen|call|check_output|check_call)", text
    )
    assert len(launches) == 1, launches  # one helper, one launch site
    body = text[text.index("def _run_git_subprocess(") : text.index("def git(")]
    assert "subprocess.run" in body  # the only launch site lives in the helper
    assert "for attempt in range(1, _GIT_MAX_ATTEMPTS + 1)" in body
    assert "time.sleep(min(0.1 * 2 ** (attempt - 1), 1.6))" in body  # 0.1s -> 1.6s
    assert "_GIT_MAX_ATTEMPTS = 5" in text
    for transient in _TRANSIENT_GIT_ERRORS:
        assert f'"{transient}"' in text, transient
    assert text.count("_run_git_subprocess(") >= 3  # def + git + git_rc


def test_ac9_transient_failure_is_retried_until_it_succeeds(tmp_path, monkeypatch):
    from swarmforge.workspace import git

    attempts: list[tuple] = []
    calls: list[float] = []
    monkeypatch.setattr("swarmforge.workspace.time.sleep", lambda s: calls.append(s))

    class Res:
        def __init__(self, code, out, err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(cmd, *a, **kw):
        attempts.append(tuple(cmd))
        return (
            Res(128, "", "unable to create thread")
            if len(attempts) < 3
            else Res(0, "7.2.1\n")
        )

    monkeypatch.setattr("swarmforge.workspace.subprocess.run", fake_run)
    assert git(tmp_path, "version").strip() == "7.2.1"
    assert len(attempts) == 3
    assert calls == [0.1, 0.2]


def test_ac10_non_transient_failure_raises_after_one_attempt(tmp_path, monkeypatch):
    from swarmforge.workspace import GitError, git

    attempts: list[tuple] = []
    calls: list[float] = []
    monkeypatch.setattr("swarmforge.workspace.time.sleep", lambda s: calls.append(s))

    class Res:
        def __init__(self, code, out, err):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(cmd, *a, **kw):
        attempts.append(tuple(cmd))
        return Res(
            128,
            "",
            "fatal: not a git repository (or any of the parent directories): .git",
        )

    monkeypatch.setattr("swarmforge.workspace.subprocess.run", fake_run)
    with pytest.raises(GitError):
        git(tmp_path, "status", check=True)
    assert len(attempts) == 1 and calls == []


def test_ac10_merge_conflict_is_never_retried(tmp_path, monkeypatch):
    from swarmforge.workspace import GitError, git

    attempts: list[int] = []
    monkeypatch.setattr("swarmforge.workspace.time.sleep", lambda s: None)

    class Res:
        def __init__(self, code, out, err):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(cmd, *a, **kw):
        attempts.append(1)
        return Res(
            1,
            "",
            "error: Your local changes to the following files would be overwritten by merge",
        )

    monkeypatch.setattr("swarmforge.workspace.subprocess.run", fake_run)
    with pytest.raises(GitError):
        git(tmp_path, "merge", "x/y", check=True)
    assert len(attempts) == 1


def test_ac10_check_false_returns_output_without_retrying(tmp_path, monkeypatch):
    from swarmforge.workspace import git

    attempts: list[int] = []
    monkeypatch.setattr("swarmforge.workspace.time.sleep", lambda s: None)

    class Res:
        def __init__(self, code, out, err):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(cmd, *a, **kw):
        attempts.append(1)
        return Res(1, "conflict", "unmerged files")

    monkeypatch.setattr("swarmforge.workspace.subprocess.run", fake_run)
    assert git(tmp_path, "merge", "x/y", check=False) == "conflict"
    assert len(attempts) == 1
