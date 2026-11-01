"""Tests for the preflight sandbox-image tool self-check (ruff/git/curl)."""

import subprocess

import pytest

from swarmforge import preflight
from swarmforge.config import parse_config
from tests.conftest import base_raw


def make_cfg(tmp_path, monkeypatch, mode="docker"):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    return parse_config(base_raw(tmp_path / "work", mode))


class FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def fake_run(missing=(), error=None):
    """Fake subprocess.run: `which <tool>` fails for tools in `missing`; raises if error."""
    calls = []

    def run(cmd, *a, **kw):
        calls.append(cmd)
        if error is not None:
            raise error(cmd)
        tool = cmd[-1]
        return FakeCompleted(1 if tool in missing else 0)

    run.calls = calls
    return run


@pytest.fixture
def docker_ok(monkeypatch):
    monkeypatch.setattr(preflight, "_docker_problem", lambda cfg, t: None)
    monkeypatch.setattr(preflight, "_image_problems", lambda cfg, img: [])


def test_missing_ruff_fail_row(tmp_path, monkeypatch, docker_ok):
    cfg = make_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(preflight.subprocess, "run", fake_run(missing=("ruff",)))
    rows = preflight.environment_check_results(cfg)
    names = [r["name"] for r in rows]
    assert names.index("Sandbox image built") < names.index("Sandbox tools")
    row = next(r for r in rows if r["name"] == "Sandbox tools")
    assert row["status"] == "fail"
    assert "missing 'ruff'" in row["message"]
    assert "pip install ruff" in row["fix"]
    assert "swarmforge sandbox build" in row["fix"]


def test_all_tools_present_ok_row(tmp_path, monkeypatch, docker_ok):
    cfg = make_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(preflight.subprocess, "run", fake_run())
    row = next(
        r
        for r in preflight.environment_check_results(cfg)
        if r["name"] == "Sandbox tools"
    )
    assert row["status"] == "ok"
    for tool in preflight.SANDBOX_REQUIRED_TOOLS:
        assert tool in row["message"]
    assert cfg.sandbox.image in row["message"]


def test_environment_problems_includes_missing_tool(tmp_path, monkeypatch, docker_ok):
    cfg = make_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(preflight.subprocess, "run", fake_run(missing=("ruff",)))
    problems = preflight.environment_problems(cfg)
    assert any("missing 'ruff'" in p and "pip install ruff" in p for p in problems)


def test_environment_problems_empty_when_healthy(tmp_path, monkeypatch, docker_ok):
    cfg = make_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(preflight.subprocess, "run", fake_run())
    assert preflight.environment_problems(cfg) == []


def test_local_mode_no_sandbox_tools_row(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, mode="local")
    run = fake_run()
    monkeypatch.setattr(preflight.subprocess, "run", run)
    rows = preflight.environment_check_results(cfg)
    assert all(r["name"] != "Sandbox tools" for r in rows)
    assert run.calls == []


def test_docker_down_skips_probe(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(
        preflight,
        "_docker_problem",
        lambda cfg, t: ("Docker is not responding.", "Start Docker."),
    )
    run = fake_run()
    monkeypatch.setattr(preflight.subprocess, "run", run)
    row = next(
        r
        for r in preflight.environment_check_results(cfg)
        if r["name"] == "Sandbox tools"
    )
    assert row["status"] == "skipped"
    assert "not checked" in row["message"]
    assert run.calls == []


def test_image_missing_skips_probe(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(preflight, "_docker_problem", lambda cfg, t: None)
    monkeypatch.setattr(
        preflight,
        "_image_problems",
        lambda cfg, img: [
            ("The sandbox image is not built yet.", "swarmforge sandbox build")
        ],
    )
    run = fake_run()
    monkeypatch.setattr(preflight.subprocess, "run", run)
    row = next(
        r
        for r in preflight.environment_check_results(cfg)
        if r["name"] == "Sandbox tools"
    )
    assert row["status"] == "skipped"
    assert "not checked" in row["message"]
    assert run.calls == []


def test_probe_failure_reported_not_raised(tmp_path, monkeypatch, docker_ok):
    cfg = make_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        fake_run(error=lambda cmd: subprocess.TimeoutExpired(cmd, 20)),
    )
    row = next(
        r
        for r in preflight.environment_check_results(cfg)
        if r["name"] == "Sandbox tools"
    )
    assert row["status"] == "fail"
    assert "could not run" in row["message"]
    assert "swarmforge sandbox build" in row["fix"]


def test_probe_oserror_reported(tmp_path, monkeypatch, docker_ok):
    cfg = make_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(
        preflight.subprocess, "run", fake_run(error=lambda cmd: OSError("no docker"))
    )
    row = next(
        r
        for r in preflight.environment_check_results(cfg)
        if r["name"] == "Sandbox tools"
    )
    assert row["status"] == "fail"
    assert "could not run" in row["message"]
