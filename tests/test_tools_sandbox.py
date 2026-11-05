"""Offline tests for the chatbot sandbox tools (local sandbox mode, tmp_path roots)."""

import asyncio
import shutil
import subprocess
import sys
import time

import pytest

from swarmforge.config import SandboxConfig
from swarmforge.tools.sandbox import ChatSandboxTools, SandboxToolError, resolve_within


def make_tools(tmp_path):
    (tmp_path / "hello.txt").write_text("hello world\n")
    return ChatSandboxTools(tmp_path, SandboxConfig(mode="local"))


def test_read_file_happy_path(tmp_path):
    tools = make_tools(tmp_path)
    assert "hello world" in tools.read_file("hello.txt")


def test_read_file_traversal_rejected(tmp_path):
    tools = make_tools(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n")
    with pytest.raises(SandboxToolError):
        tools.read_file("../outside.txt")
    with pytest.raises(SandboxToolError):
        tools.read_file(str(outside))


def test_read_file_symlink_escape_rejected(tmp_path):
    tools = make_tools(tmp_path)
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret\n")
    (tmp_path / "link.txt").symlink_to(outside)
    with pytest.raises(SandboxToolError):
        tools.read_file("link.txt")


def test_read_file_truncation(tmp_path):
    tools = make_tools(tmp_path)
    (tmp_path / "big.txt").write_text("x" * 100)
    out = tools.read_file("big.txt", max_chars=10)
    assert out == "x" * 10 + "\n... [truncated]"


def test_run_command_stdout_and_stderr(tmp_path):
    tools = make_tools(tmp_path)
    cmd = f"\"{sys.executable}\" -c \"print('out'); import sys; print('err', file=sys.stderr)\""
    out = asyncio.run(tools.run_command(cmd))
    assert "out" in out and "err" in out
    assert "[exit 0" in out


def test_run_command_timeout(tmp_path):
    tools = make_tools(tmp_path)
    start = time.monotonic()
    out = asyncio.run(
        tools.run_command(
            f'"{sys.executable}" -c "import time;time.sleep(10)"', timeout_s=1
        )
    )
    assert "TIMED OUT" in out
    assert time.monotonic() - start < 8


def test_run_command_timeout_message_uses_clamped_value(tmp_path):
    tools = make_tools(tmp_path)
    start = time.monotonic()
    out = asyncio.run(
        tools.run_command(
            f'"{sys.executable}" -c "import time;time.sleep(5)"', timeout_s=0
        )
    )
    assert "[TIMED OUT after 1s]" in out
    assert time.monotonic() - start < 5


def test_run_command_timeout_message_uses_clamped_value_for_negative(tmp_path):
    tools = make_tools(tmp_path)
    start = time.monotonic()
    out = asyncio.run(
        tools.run_command(
            f'"{sys.executable}" -c "import time;time.sleep(5)"', timeout_s=-3
        )
    )
    assert "[TIMED OUT after 1s]" in out
    assert "[TIMED OUT after -3s]" not in out
    assert time.monotonic() - start < 5


def test_run_command_blank_rejected(tmp_path):
    tools = make_tools(tmp_path)
    with pytest.raises(SandboxToolError):
        asyncio.run(tools.run_command("   "))


def test_grep_finds_pattern_with_line_numbers(tmp_path):
    tools = make_tools(tmp_path)
    (tmp_path / "code.py").write_text("alpha\nbeta\ngamma\n")
    out = tools.grep("beta")
    assert "code.py:2:beta" in out


def test_grep_truncation_200_lines(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmforge.tools.sandbox.shutil.which", lambda name: None)
    tools = make_tools(tmp_path)
    (tmp_path / "many.txt").write_text("match\n" * 300)
    out = tools.grep("match")
    lines = out.splitlines()
    assert len(lines) == 201
    assert lines[-1] == "... [truncated: showing first 200 matches]"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_with_rg(tmp_path):
    tools = make_tools(tmp_path)
    (tmp_path / "code.py").write_text("alpha\nbeta\n")
    out = tools.grep("beta")
    assert "code.py:2:beta" in out


def test_grep_invalid_regex(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmforge.tools.sandbox.shutil.which", lambda name: None)
    tools = make_tools(tmp_path)
    with pytest.raises(SandboxToolError):
        tools.grep("([unclosed")


def test_grep_no_matches(tmp_path):
    tools = make_tools(tmp_path)
    assert "No matches" in tools.grep("zzz-not-present")


def test_resolve_within_rejects_missing(tmp_path):
    with pytest.raises(SandboxToolError):
        resolve_within(tmp_path, "nope.txt")
    with pytest.raises(SandboxToolError):
        resolve_within(tmp_path, "  ")


def test_chat_docker_mount_is_read_only(tmp_path):
    from swarmforge.tools.sandbox import ReadOnlyChatSandbox

    args = ReadOnlyChatSandbox(SandboxConfig()).build_args(
        tmp_path, "echo ok", "test", network="none"
    )
    mount = next(
        args[i + 1]
        for i, value in enumerate(args)
        if value == "--mount" and "target=/workspace" in args[i + 1]
    )
    assert mount.endswith(",readonly")
    assert args[args.index("--network") + 1] == "none"


def test_shell_timeout_is_capped_and_network_disabled(tmp_path, monkeypatch):
    from swarmforge.tools import sandbox as module
    from swarmforge.sandbox import CommandResult

    seen = {}

    class Stub:
        async def run(self, root, command, **kwargs):
            seen.update(kwargs)
            return CommandResult(command, 0, "ok", 0.1)

    monkeypatch.setattr(module, "make_sandbox", lambda _: Stub())
    asyncio.run(ChatSandboxTools(tmp_path).run_command("echo ok", timeout_s=1000))
    assert seen == {"timeout_s": 30, "network": "none"}


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is not installed")
def test_chat_command_cannot_modify_project_in_docker(tmp_path):
    original = tmp_path / "original.txt"
    original.write_text("keep me")
    tools = ChatSandboxTools(tmp_path, SandboxConfig(mode="docker"))
    output = asyncio.run(
        tools.run_command(
            "python -c \"from pathlib import Path; Path('original.txt').write_text('changed')\""
        )
    )
    assert "[exit 0" not in output
    assert original.read_text() == "keep me"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_rg_runs_single_threaded(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(
            argv, 0, stdout="code.py:2:beta\n", stderr=""
        )

    monkeypatch.setattr("swarmforge.tools.sandbox.subprocess.run", fake_run)
    tools = make_tools(tmp_path)
    (tmp_path / "code.py").write_text("alpha\nbeta\n")
    out = tools.grep("beta")
    assert "--threads=1" in captured["argv"]
    assert "code.py:2:beta" in out


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_rg_timeout_falls_back_to_python_grep(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr("swarmforge.tools.sandbox.subprocess.run", fake_run)
    tools = make_tools(tmp_path)
    (tmp_path / "code.py").write_text("alpha\nbeta\n")
    out = tools.grep("beta")
    assert "code.py:2:beta" in out


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_rg_error_exit_falls_back_to_python_grep(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 101, stdout="", stderr="panic")

    monkeypatch.setattr("swarmforge.tools.sandbox.subprocess.run", fake_run)
    tools = make_tools(tmp_path)
    (tmp_path / "code.py").write_text("alpha\nbeta\n")
    out = tools.grep("beta")
    assert "code.py:2:beta" in out
