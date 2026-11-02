"""Acceptance tests for the sandbox-image self-check added to preflight.

Issue: "Add sandbox image self-check to preflight (ruff, git, curl)".

* AC1  docker mode probes `docker run --rm <image> which <tool>` for every required tool and
       reports a failing 'Sandbox tools' row naming each missing tool with the one-line
       Dockerfile install hint (`pip install ruff`).
* AC2  (revised) a missing tool is a WARNING in the run log, never a reason to block: agents work
       without ruff/git/curl, and an image older than the Dockerfile once blocked every queued task.
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


def test_missing_tool_is_a_warning_not_a_blocker(tmp_path, monkeypatch, docker_env_ok):
    cfg = _docker_cfg(tmp_path, monkeypatch)
    run = make_docker_run(missing=("ruff",))

    def fake(cmd, *a, **kw):
        if cmd[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="sha256:img1\n", stderr="")
        return run(cmd, *a, **kw)
    monkeypatch.setattr(preflight.subprocess, "run", fake)
    monkeypatch.setattr(preflight, "_TOOL_CHECKS", {})
    assert preflight.environment_problems(cfg) == []
    warnings = preflight.sandbox_tool_warnings(cfg)
    assert any("missing 'ruff'" in w and "pip install ruff" in w for w in warnings)
    probes = len(run.calls)
    assert preflight.sandbox_tool_warnings(cfg) == warnings and len(run.calls) == probes   # once per image


# ------------------------------------------------------------------ AC2: fail fast


async def test_orchestrator_logs_missing_tools_and_keeps_going(cfg, sample_repo, monkeypatch):
    """A sandbox image without ruff/git/curl must not stop the run: it is logged as a warning."""
    from swarmforge import orchestrator as orch_mod

    from . import fake_team
    monkeypatch.setattr(orch_mod, "sandbox_tool_warnings", lambda c, img: [
        "The sandbox image 'swarmforge-sandbox:latest' is missing 'git'. install the tool ..."])
    provider = FakeProvider({"qwen-fake": fake_team.planner, "deepseek-fake": fake_team.developer,
                             "kimi-fake": fake_team.developer, "minimax-fake": fake_team.tester,
                             "glm-fake": fake_team.Reviewer()})
    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, echo=None, llm_transport=provider.transport)
    run_id = await orch.execute(issue_file=str(ISSUE), repo=str(sample_repo))
    run = store.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    assert any(e["kind"] == "environment" and e["level"] == "warn" and "not blocking" in e["message"]
               for e in store.events(run_id))


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
