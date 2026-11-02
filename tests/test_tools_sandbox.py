"""Offline tests for the chatbot sandbox tools (local sandbox mode, tmp_path roots)."""

import asyncio
import shutil
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
    start = time.time()
    out = asyncio.run(
        tools.run_command(
            f'"{sys.executable}" -c "import time;time.sleep(10)"', timeout_s=1
        )
    )
    assert "TIMED OUT" in out
    assert time.time() - start < 8


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
