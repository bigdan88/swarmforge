"""Tests for the transient-error retry helper around git subprocesses."""
from __future__ import annotations

from pathlib import Path

import pytest

from swarmforge import workspace
from swarmforge.workspace import GitError, git, git_rc


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_git_retries_transient_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    results = [
        FakeCompleted(1, stderr="fatal: unable to create thread"),
        FakeCompleted(1, stderr="fatal: unable to create thread"),
        FakeCompleted(0, stdout="ok"),
    ]

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return results[len(calls) - 1]

    sleeps: list[float] = []
    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    monkeypatch.setattr(workspace.time, "sleep", lambda s: sleeps.append(s))

    assert git(tmp_path, "status") == "ok"
    assert len(calls) == 3
    assert sleeps == [0.1, 0.2]


def test_git_non_transient_fails_once(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(1, stderr="merge conflict in src/x.py")

    sleeps: list[float] = []
    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    monkeypatch.setattr(workspace.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(GitError) as exc:
        git(tmp_path, "merge", "feature")
    assert "merge conflict in src/x.py" in str(exc.value)
    assert len(calls) == 1
    assert sleeps == []


def test_git_rc_retries_transient(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    results = [
        FakeCompleted(1, stderr="fatal: cannot fork"),
        FakeCompleted(0, stdout="branch info"),
    ]

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return results[len(calls) - 1]

    sleeps: list[float] = []
    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    monkeypatch.setattr(workspace.time, "sleep", lambda s: sleeps.append(s))

    rc, out = git_rc(tmp_path, "branch", "--show-current")
    assert rc == 0
    assert out == "branch info"
    assert len(calls) == 2
    assert sleeps == [0.1]


def test_git_check_false_returns_output(tmp_path: Path, monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return FakeCompleted(1, stdout="out", stderr="err")

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    assert git(tmp_path, "status", check=False) == "out"
