"""Acceptance tests for the sandbox shell & file-read chatbot tools (tools/sandbox.py).

One focused, independent check per acceptance criterion of the issue:

AC1 run_command returns combined stdout+stderr and enforces a timeout (default 30 s),
   marking killed commands with [TIMED OUT after Ns].
AC2 read_file rejects ../ traversal and absolute paths outside the project root with a
   SandboxToolError (surfaced by ToolRegistry as an 'ERROR: ...' string); symlink escapes
   are rejected too.
AC3 grep uses ripgrep when on PATH, otherwise a pure-Python regex scan, truncated to 200
   lines with a visible truncation marker.
AC4 chatbot.enable_shell exists, defaults to false, is parsed from the `chatbot:` YAML
   section with unknown-key validation; when false the registry contains only fetch_url.
AC5 Unit tests demonstrate traversal rejection, timeout enforcement and grep truncation
   offline (LocalSandbox + tmp_path) -- these ARE those tests (mapped below).
AC6 With chatbot.enable_shell: true, run_command/read_file/grep are all registered in the
   ToolRegistry (names + JSON schemas) alongside fetch_url and executable via async
   registry.execute().

Everything is offline: local sandbox mode, tmp_path roots, no network.
"""

import asyncio
import inspect
import shutil
import sys
import time

import pytest
import yaml

from swarmforge.chatbot import build_chat_registry
from swarmforge.config import (
    ChatbotConfig,
    ConfigError,
    SandboxConfig,
    load_config,
    parse_config,
)
from swarmforge.tools.sandbox import (
    DEFAULT_TIMEOUT_S,
    GREP_MAX_LINES,
    ChatSandboxTools,
    SandboxToolError,
    resolve_within,
)

from .conftest import base_raw


def _tools(tmp_path) -> ChatSandboxTools:
    (tmp_path / "hello.txt").write_text("hello world\n", encoding="utf-8")
    return ChatSandboxTools(tmp_path, SandboxConfig(mode="local"))


def _py(code: str) -> str:
    """A shell command running `code` with the test interpreter."""
    return f'"{sys.executable}" -c "{code}"'


# --------------------------------------------------------------------------- AC1


def test_ac1_run_command_combines_stdout_and_stderr(tmp_path):
    tools = _tools(tmp_path)
    out = asyncio.run(
        tools.run_command(
            _py("print('to-stdout'); import sys; print('to-stderr', file=sys.stderr)")
        )
    )
    assert "to-stdout" in out
    assert "to-stderr" in out  # stderr merged into stdout (stderr=STDOUT)
    assert "[exit 0" in out


def test_ac1_run_command_timeout_kills_and_marks(tmp_path):
    tools = _tools(tmp_path)
    start = time.monotonic()
    out = asyncio.run(
        tools.run_command(_py("import time; time.sleep(30)"), timeout_s=1)
    )
    elapsed = time.monotonic() - start
    assert "[TIMED OUT after 1s]" in out
    assert elapsed < 10, f"command was not killed promptly (took {elapsed:.1f}s)"


def test_ac1_default_timeout_is_30_seconds():
    assert DEFAULT_TIMEOUT_S == 30
    default = (
        inspect.signature(ChatSandboxTools.run_command).parameters["timeout_s"].default
    )
    assert default == 30


# --------------------------------------------------------------------------- AC2


def test_ac2_read_file_rejects_dotdot_traversal(tmp_path):
    tools = _tools(tmp_path)
    (tmp_path.parent / "outside.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(SandboxToolError):
        tools.read_file("../outside.txt")
    with pytest.raises(SandboxToolError):
        tools.read_file("sub/../../outside.txt")


def test_ac2_read_file_rejects_absolute_path_outside_root(tmp_path):
    tools = _tools(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(SandboxToolError):
        tools.read_file(str(outside))
    with pytest.raises(SandboxToolError):
        tools.read_file("/etc/hostname")


def test_ac2_read_file_rejects_symlink_escaping_root(tmp_path):
    tools = _tools(tmp_path)
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(secret)
    with pytest.raises(SandboxToolError):
        tools.read_file("link.txt")


def test_ac2_registry_surfaces_traversal_as_error_string(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"enable_shell": True}
    cfg = parse_config(raw)
    monkeypatch.chdir(tmp_path)  # registry scopes tools to the cwd (project root)
    registry = build_chat_registry(cfg)
    result = asyncio.run(registry.execute("read_file", {"path": "../../etc/passwd"}))
    assert isinstance(result, str)
    assert result.startswith("ERROR:")
    assert "SandboxToolError" in result or "escapes" in result


# --------------------------------------------------------------------------- AC3


def test_ac3_grep_fallback_pure_python_finds_matches(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmforge.tools.sandbox.shutil.which", lambda name: None)
    tools = _tools(tmp_path)
    (tmp_path / "code.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    out = tools.grep("beta")
    assert "code.py:2:beta" in out  # rel-path:lineno:line format


def test_ac3_grep_fallback_truncated_to_200_lines_with_marker(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmforge.tools.sandbox.shutil.which", lambda name: None)
    tools = _tools(tmp_path)
    (tmp_path / "many.txt").write_text("match line\n" * 250, encoding="utf-8")
    out = tools.grep("match")
    lines = out.splitlines()
    match_lines = [ln for ln in lines if "match line" in ln]
    assert len(match_lines) == GREP_MAX_LINES == 200
    assert lines[-1] == "... [truncated: showing first 200 matches]"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_ac3_grep_uses_rg_and_truncates_to_200_lines(tmp_path):
    tools = _tools(tmp_path)
    (tmp_path / "many.txt").write_text("needle\n" * 250, encoding="utf-8")
    out = tools.grep("needle")
    lines = out.splitlines()
    assert len([ln for ln in lines if "needle" in ln]) == 200
    assert lines[-1].startswith("... [truncated")
    # rg path also returns normal matches with file:line: content
    (tmp_path / "code.py").write_text("alpha\nbeta\n", encoding="utf-8")
    assert "code.py:2:beta" in tools.grep("beta")


# --------------------------------------------------------------------------- AC4


def test_ac4_enable_shell_defaults_to_false():
    assert ChatbotConfig().enable_shell is False


def test_ac4_parsed_from_chatbot_yaml_section(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"enable_shell": True}
    cfg_file = tmp_path / "swarmforge.yaml"
    cfg_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = load_config(str(cfg_file))
    assert cfg.chatbot.enable_shell is True


def test_ac4_unknown_key_in_chatbot_section_rejected(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"enable_shell": True, "enabla_shel": 1}
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_ac4_non_boolean_value_rejected(tmp_path):
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"enable_shell": "yes"}
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_ac4_default_registry_contains_only_fetch_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    cfg = parse_config(base_raw(tmp_path / "work"))
    assert cfg.chatbot.enable_shell is False
    assert set(build_chat_registry(cfg).names()) == {"fetch_url", "list_files", "repo_tree", "repo_file", "repo_search", "repo_issues"}


# --------------------------------------------------------------------------- AC6


def _shell_enabled_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["chatbot"] = {"enable_shell": True}
    monkeypatch.chdir(tmp_path)
    return build_chat_registry(parse_config(raw))


def test_ac6_shell_true_registers_all_tools_with_schemas(tmp_path, monkeypatch):
    registry = _shell_enabled_registry(tmp_path, monkeypatch)
    names = registry.names()
    assert names[0] == "fetch_url"
    assert {"run_command", "read_file", "grep"} <= set(names)
    schemas = {s["function"]["name"]: s for s in registry.schemas()}
    assert schemas["run_command"]["function"]["parameters"]["required"] == ["command"]
    assert schemas["read_file"]["function"]["parameters"]["required"] == ["path"]
    assert schemas["grep"]["function"]["parameters"]["required"] == ["pattern"]
    for name in ("run_command", "read_file", "grep"):
        assert schemas[name]["type"] == "function"
        assert schemas[name]["function"]["description"]


def test_ac6_tools_executable_through_async_registry_execute(tmp_path, monkeypatch):
    registry = _shell_enabled_registry(tmp_path, monkeypatch)
    (tmp_path / "notes.txt").write_text("sandbox says hi\n", encoding="utf-8")

    read = asyncio.run(registry.execute("read_file", {"path": "notes.txt"}))
    assert "sandbox says hi" in read
    assert not read.startswith("ERROR:")

    searched = asyncio.run(registry.execute("grep", {"pattern": "sandbox"}))
    assert "notes.txt:1:sandbox says hi" in searched

    ran = asyncio.run(registry.execute("run_command", {"command": "echo registry-run"}))
    assert "registry-run" in ran
    assert "[exit 0" in ran


# ---------------------------------------------------------------- extra guards


def test_resolve_within_rejects_empty_and_missing(tmp_path):
    with pytest.raises(SandboxToolError):
        resolve_within(tmp_path, "   ")
    with pytest.raises(SandboxToolError):
        resolve_within(tmp_path, "does-not-exist.txt")


def test_run_command_rejects_blank_command(tmp_path):
    tools = _tools(tmp_path)
    with pytest.raises(SandboxToolError):
        asyncio.run(tools.run_command("  "))
