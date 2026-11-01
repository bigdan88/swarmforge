"""Acceptance tests for the sandbox-image self-check added to preflight.

Issue: "Add sandbox image self-check to preflight (ruff, git, curl)".

* AC1  docker mode probes `docker run --rm <image> which <tool>` for every required tool and
       reports a failing 'Sandbox tools' row naming each missing tool with the one-line
       Dockerfile install hint (`pip install ruff`).
* AC2  environment_problems() carries the finding so the orchestrator aborts the run at
       preflight (BLOCKED, zero LLM calls, zero repair cycles) and the dashboard Start-run
       button shows the error instead of launching a doomed run.
* AC4  every tool referenced in prompts.py (incl. start_process) is registered.
* AC5  the Settings & health page treats 'Sandbox tools' as a canonical check (skipped
       "not checked" row in local mode, real probe result in docker mode, no fabricated rows).
* AC6  the shipped sandbox Dockerfile installs every self-checked tool.
"""

import subprocess
from pathlib import Path

import pytest

from swarmforge import preflight, prompts
from swarmforge.agent import tools as agent_tools
from swarmforge.config import load_config, parse_config
from swarmforge.db import Store
from swarmforge.orchestrator import Orchestrator
from swarmforge.settings import CANONICAL_CHECKS, get_health_checks

from .conftest import ISSUE, FakeProvider, base_raw

DOCKERFILE = Path(preflight.__file__).resolve().parent / "resources" / "Dockerfile.sandbox"


def _docker_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    return parse_config(base_raw(tmp_path / "work", "docker"))


def make_docker_run(missing=()):
    """Fake subprocess.run: answers `docker ... which <tool>`; delegates everything else
    (e.g. git calls made by the orchestrator's repo preflight) to the real subprocess.run."""
    real = subprocess.run
    calls: list[list[str]] = []

    def run(cmd, *a, **kw):
        argv = [str(x) for x in cmd]
        if argv and argv[0] == "docker":
            calls.append(argv)
            rc = 1 if argv[-1] in missing else 0
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")
        return real(cmd, *a, **kw)

    run.calls = calls
    return run


@pytest.fixture
def docker_env_ok(monkeypatch):
    """Docker binary/daemon and the image itself are fine; only `which` probes are faked."""
    monkeypatch.setattr(preflight, "_docker_problem", lambda cfg, t: None)
    monkeypatch.setattr(preflight, "_image_problems", lambda cfg, img: [])


# ------------------------------------------------------------------ AC1: the probe itself


def test_required_tools_are_ruff_git_curl():
    assert set(preflight.SANDBOX_REQUIRED_TOOLS) == {"ruff", "git", "curl"}
    assert set(preflight.SANDBOX_TOOL_INSTALL_HINTS) == set(preflight.SANDBOX_REQUIRED_TOOLS)
    assert preflight.SANDBOX_TOOL_INSTALL_HINTS["ruff"] == "pip install ruff"


def test_probe_runs_docker_run_rm_image_which_tool(tmp_path, monkeypatch, docker_env_ok):
    cfg = _docker_cfg(tmp_path, monkeypatch)
    run = make_docker_run()
    monkeypatch.setattr(preflight.subprocess, "run", run)
    rows = preflight.environment_check_results(cfg)
    docker = cfg.sandbox.docker_binary
    img = cfg.sandbox.image
    for tool in preflight.SANDBOX_REQUIRED_TOOLS:
        assert [docker, "run", "--rm", img, "which", tool] in run.calls, tool
    assert all(r["status"] == "ok" for r in rows)


def test_missing_tool_row_names_tool_and_gives_install_hint(
    tmp_path, monkeypatch, docker_env_ok
):
    cfg = _docker_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(preflight.subprocess, "run", make_docker_run(missing=("ruff",)))
    row = next(r for r in preflight.environment_check_results(cfg) if r["name"] == "Sandbox tools")
    assert row["status"] == "fail"
    assert "missing 'ruff'" in row["message"]
    assert cfg.sandbox.image in row["message"]
    # one-line fix hint for the image Dockerfile, plus the rebuild command
    assert "pip install ruff" in row["fix"]
    assert "swarmforge sandbox build" in row["fix"]


def test_multiple_missing_tools_all_named_with_their_hints(
    tmp_path, monkeypatch, docker_env_ok
):
    cfg = _docker_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(preflight.subprocess, "run", make_docker_run(missing=("git", "curl")))
    row = next(r for r in preflight.environment_check_results(cfg) if r["name"] == "Sandbox tools")
    assert row["status"] == "fail"
    assert "missing 'git'" in row["message"]
    assert "missing 'curl'" in row["message"]
    assert "ruff" not in row["message"]  # present tools are not reported
    assert "apt-get install -y git" in row["fix"]
    assert "apt-get install -y curl" in row["fix"]
    assert "pip install ruff" not in row["fix"]


def test_environment_problems_carries_missing_tool_finding(
    tmp_path, monkeypatch, docker_env_ok
):
    cfg = _docker_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(preflight.subprocess, "run", make_docker_run(missing=("ruff",)))
    problems = preflight.environment_problems(cfg)
    assert any("missing 'ruff'" in p and "pip install ruff" in p for p in problems)
    monkeypatch.setattr(preflight.subprocess, "run", make_docker_run())
    assert preflight.environment_problems(cfg) == []


# ------------------------------------------------------------------ AC2: fail fast


async def test_orchestrator_aborts_at_preflight_when_tool_missing(
    cfg, sample_repo, monkeypatch, docker_env_ok
):
    """A broken sandbox stops the run at REPOSITORY_INSPECTION/preflight: BLOCKED state,
    the finding in state_reason, and not a single LLM call (no repair cycles burned)."""
    cfg.sandbox.mode = "docker"
    # simulate a host where the docker binary exists (make_sandbox only checks shutil.which)
    import shutil as _shutil

    real_which = _shutil.which
    monkeypatch.setattr(
        _shutil, "which",
        lambda name: f"/usr/bin/{name}" if name == cfg.sandbox.docker_binary else real_which(name),
    )
    monkeypatch.setattr(preflight.subprocess, "run", make_docker_run(missing=("ruff",)))
    provider = FakeProvider({})
    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, echo=None, llm_transport=provider.transport)
    run_id = await orch.execute(issue_file=str(ISSUE), repo=str(sample_repo))
    run = store.get_run(run_id)
    assert run["state"] == "BLOCKED", run["state_reason"]
    reason = run["state_reason"] or ""
    assert "missing 'ruff'" in reason
    assert "pip install ruff" in reason
    assert provider.requests == []
    assert store.cost_summary(run_id)["calls"] == 0
    assert store.tasks(run_id) == []  # nothing was ever planned or dispatched


def test_dashboard_start_run_aborts_on_missing_sandbox_tool(tmp_path, monkeypatch, sample_repo):
    """AC2 (dashboard side): Start run shows the finding and does not launch a doomed run."""
    import streamlit as st
    import yaml

    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    st.cache_resource.clear()
    raw = base_raw(tmp_path / "work", "docker")
    path = tmp_path / "swarmforge.yaml"
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    for k in ("SWARMFORGE_CONFIG", "SWARMFORGE_DASH_AUTOREFRESH", "SWARMFORGE_DASH_UPDATE_CHECK"):
        monkeypatch.setenv(k, "0" if k != "SWARMFORGE_CONFIG" else str(path))
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    cfg = load_config(str(path))
    store = Store(cfg.db_path)
    store.remember_repo(str(sample_repo))
    monkeypatch.setattr(preflight, "_docker_problem", lambda cfg, t: None)
    monkeypatch.setattr(preflight, "_image_problems", lambda cfg, img: [])
    monkeypatch.setattr(preflight.subprocess, "run", make_docker_run(missing=("ruff",)))

    app = Path(preflight.__file__).resolve().parent / "dashboard.py"
    at = AppTest.from_file(str(app), default_timeout=90)
    at.run()
    at.button(key="nav_new-task").click().run()
    at.text_input(key="nr_title").input("Add export")
    at.text_area(key="nr_body").input("Add CSV export with tests.")
    at.selectbox(key="nr_repo").set_value(str(sample_repo))
    next(b for b in at.button if "Start run" in b.label).click().run()

    assert not at.exception, [e.value for e in at.exception]
    errors = " ".join(e.value for e in at.error)
    assert "missing 'ruff'" in errors, errors
    assert "pip install ruff" in errors, errors
    assert store.list_runs() == []  # the run was never started


# ------------------------------------------------------------------ AC3 (beyond dev tests)


def test_local_mode_never_spawns_docker(tmp_path, monkeypatch):
    cfg = parse_config(base_raw(tmp_path / "work", "local"))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    run = make_docker_run()
    monkeypatch.setattr(preflight.subprocess, "run", run)
    rows = preflight.environment_check_results(cfg)
    assert run.calls == []
    assert preflight.environment_problems(cfg) == []
    assert all(r["name"] not in ("Docker running", "Sandbox image built", "Sandbox tools")
               for r in rows)


# ------------------------------------------------------------------ AC4: prompts vs registry


def test_start_process_is_registered_and_referenced():
    # the stale reference from the issue: prompts mention start_process; it must exist now
    assert "start_process" in prompts.TESTER_SYSTEM
    assert "start_process" in agent_tools.TOOL_SCHEMAS
    assert "start_process" in agent_tools.ASYNC_IMPLS


# ------------------------------------------------------------------ AC5: health page rows


def test_health_checks_local_mode_skips_sandbox_tools(tmp_path, monkeypatch):
    cfg = parse_config(base_raw(tmp_path / "work", "local"))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    rows = get_health_checks(cfg)
    names = [r["name"] for r in rows]
    assert "Sandbox tools" in names
    row = next(r for r in rows if r["name"] == "Sandbox tools")
    assert row["status"] == "skipped"
    assert "not checked" in row["message"]
    assert row["optional"] is True
    # no fabricated rows: every check is canonical
    assert set(names) <= set(CANONICAL_CHECKS) | {"GitHub access"}


def test_health_checks_docker_mode_shows_real_failing_row(tmp_path, monkeypatch, docker_env_ok):
    cfg = _docker_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(preflight.subprocess, "run", make_docker_run(missing=("ruff",)))
    rows = get_health_checks(cfg)
    row = next(r for r in rows if r["name"] == "Sandbox tools")
    assert row["status"] == "fail"
    assert "missing 'ruff'" in row["message"]
    assert "pip install ruff" in row["fix"]
    assert row["optional"] is False
    assert set(r["name"] for r in rows) <= set(CANONICAL_CHECKS) | {"GitHub access"}


# ------------------------------------------------------------------ AC6: Dockerfile pins


def test_dockerfile_installs_every_self_checked_tool():
    text = DOCKERFILE.read_text(encoding="utf-8")
    install_lines = " ".join(
        ln.strip().lstrip("\\").strip() for ln in text.splitlines()
        if "apt-get install" in ln or "pip install" in ln
    )
    for tool in preflight.SANDBOX_REQUIRED_TOOLS:
        assert tool in install_lines, f"{tool} not installed by the sandbox Dockerfile"
