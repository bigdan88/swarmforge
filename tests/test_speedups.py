"""Run-time speedups: one active run per repository, run_command in the agent's session container,
targeted-test hints for agents."""
import asyncio
import os
import time

import pytest

from swarmforge.agent import devtools
from swarmforge.agent.tools import ToolContext, run_command
from swarmforge.config import SandboxConfig
from swarmforge.db import Store
from swarmforge.orchestrator import Orchestrator, RunAborted
from swarmforge.repolock import blocking_runs, repo_key
from swarmforge.sandbox import CommandResult, DockerSandbox


@pytest.mark.parametrize("repo,issue,key", [
    ("bigdan88/swarmforge", None, "bigdan88/swarmforge"),
    ("https://github.com/bigdan88/swarmforge.git", None, "bigdan88/swarmforge"),
    ("https://github.com/BigDan88/SwarmForge/tree/main", None, "bigdan88/swarmforge"),
    ("git@github.com:bigdan88/swarmforge.git", None, "bigdan88/swarmforge"),
    (None, "https://github.com/bigdan88/swarmforge/issues/42", "bigdan88/swarmforge"),
    ("", None, None),
])
def test_repo_key_normalizes_the_same_repository(repo, issue, key):
    assert repo_key(repo, issue) == key


def test_local_paths_resolve(tmp_path):
    assert repo_key(str(tmp_path)) == repo_key(str(tmp_path) + "/") == str(tmp_path.resolve()).lower()


def _store(tmp_path):
    return Store(tmp_path / "db.sqlite")


def test_blocking_runs_only_earlier_active_alive_same_repo(tmp_path):
    st = _store(tmp_path)
    st.create_run("r1", repo="https://github.com/bigdan88/swarmforge.git", state="PLANNING", pid=1)
    time.sleep(0.01)
    st.create_run("r2", repo="bigdan88/other", state="PLANNING", pid=1)
    st.create_run("r3", repo="bigdan88/swarmforge", state="COMPLETED", pid=1)
    st.create_run("r4", repo="bigdan88/swarmforge", state="PLANNING", pid=999)      # process is gone
    time.sleep(0.01)
    st.create_run("me", repo="bigdan88/swarmforge", pid=1)
    time.sleep(0.01)
    st.create_run("later", repo="bigdan88/swarmforge", state="PLANNING", pid=1)
    alive = lambda pid: pid == 1  # noqa: E731
    assert [r["id"] for r in blocking_runs(st, "me", "bigdan88/swarmforge", alive)] == ["r1"]
    assert blocking_runs(st, "r1", "bigdan88/swarmforge", alive) == []          # first come, first served


def _orch(cfg, st, run_id):
    o = Orchestrator(cfg, st, echo=None)
    o.run_id = run_id
    o._repo_poll_s = 0.05
    return o


async def test_run_waits_for_earlier_run_on_same_repo(cfg, tmp_path):
    st = _store(tmp_path)
    st.create_run("first", repo="bigdan88/swarmforge", state="TASK_EXECUTION", pid=os.getpid())
    time.sleep(0.01)
    st.create_run("second", pid=os.getpid())
    o = _orch(cfg, st, "second")
    waiter = asyncio.ensure_future(o._wait_for_repo("https://github.com/bigdan88/swarmforge.git", None))
    await asyncio.sleep(0.3)
    assert not waiter.done()
    assert any("waiting: run(s) first" in e["message"] for e in st.events("second"))
    assert st.get_run("second")["state_reason"].startswith("waiting for the repository: run(s) first")
    st.update_run("first", state="COMPLETED")
    await asyncio.wait_for(waiter, 5)
    assert st.get_run("second")["state_reason"] is None
    assert any("repository is free" in e["message"] for e in st.events("second"))


async def test_waiting_run_can_be_cancelled_and_setting_disables_waiting(cfg, tmp_path):
    st = _store(tmp_path)
    st.create_run("first", repo="bigdan88/swarmforge", state="PLANNING", pid=os.getpid())
    time.sleep(0.01)
    st.create_run("second", pid=os.getpid())
    o = _orch(cfg, st, "second")
    st.request_cancel("second")
    with pytest.raises(RunAborted):
        await asyncio.wait_for(o._wait_for_repo("bigdan88/swarmforge", None), 5)
    o2 = _orch(cfg, st, "second")
    o2.cfg.limits.one_run_per_repo = False
    await asyncio.wait_for(o2._wait_for_repo("bigdan88/swarmforge", None), 1)


class FakeDocker(DockerSandbox):
    def __init__(self, reuse):
        super().__init__(SandboxConfig(reuse_container=reuse))
        self.fresh = 0

    async def run(self, workspace, command, **kw):
        self.fresh += 1
        return CommandResult(command, 0, "fresh container", 0.1)


async def test_run_command_uses_the_session_container_when_enabled(tmp_path, monkeypatch):
    calls = []

    async def fake_session_run(ctx, command, t):
        calls.append((command, t))
        return CommandResult(command, 0, "session", 0.1)
    monkeypatch.setattr(devtools, "session_run", fake_session_run)
    sb = FakeDocker(reuse=True)
    ctx = ToolContext(workspace=tmp_path, sandbox=sb, command_timeout_s=100)
    out = await run_command(ctx, "pytest -q", timeout_s=500)
    assert "session" in out and calls == [("pytest -q", 100)] and sb.fresh == 0
    sb2 = FakeDocker(reuse=False)
    out = await run_command(ToolContext(workspace=tmp_path, sandbox=sb2), "pytest -q")
    assert "fresh container" in out and sb2.fresh == 1


async def test_session_run_enforces_timeout_inside_the_container(tmp_path):
    class FakeSession(devtools.Session):
        async def exec(self, command, *, timeout_s=60, stdin=None, env=None):
            self.seen = (command, env, timeout_s)
            return CommandResult(command, 124, "", 3.0)
    ctx = ToolContext(workspace=tmp_path, sandbox=FakeDocker(True))
    ctx.session = FakeSession()
    r = await devtools.session_run(ctx, "sleep 999", 7)
    cmd, env, t = ctx.session.seen
    assert cmd.startswith("timeout ") and env == {"SF_CMD": "sleep 999", "SF_T": "7"} and t > 7
    assert r.timed_out and r.command == "sleep 999"


def test_agents_are_told_to_run_targeted_tests(cfg, tmp_path):
    o = _orch(cfg, _store(tmp_path), "x")
    hint = o._test_hint()
    assert o.cfg.project.test_command in hint and "relevant to your change" in hint and "at most once" in hint
    o.cfg.project.agent_test_command = 'pytest -m "not slow"'
    assert 'quick suite `pytest -m "not slow"`' in o._test_hint()


def test_agent_env_reaches_agent_sessions_only(tmp_path):
    from swarmforge.agent.devtools import DockerSession, LocalSession
    from swarmforge.sandbox import LocalSandbox
    sb = DockerSandbox(SandboxConfig())
    sb.agent_env = {"PYTEST_ADDOPTS": "-m 'not slow'"}
    args = DockerSession(sb, tmp_path, None).run_args()
    assert "PYTEST_ADDOPTS=-m 'not slow'" in args
    assert "PYTEST_ADDOPTS" not in " ".join(sb.build_args(tmp_path, "pytest", "x"))   # evidence runs: full suite
    lsb = LocalSandbox(SandboxConfig(mode="local"))
    lsb.agent_env = {"PYTEST_ADDOPTS": "-q"}
    assert LocalSession(lsb, tmp_path, None)._env()["PYTEST_ADDOPTS"] == "-q"



@pytest.mark.slow
async def test_profile_agent_env_and_baseline_is_reused_for_the_same_commit(cfg, sample_repo):
    import subprocess
    from tests.conftest import ISSUE
    from tests.test_e2e import _provider
    (sample_repo / "swarmforge.project.yml").write_text('agent_env:\n  PYTEST_ADDOPTS: "-p no:randomly"\n')
    subprocess.run(["git", "add", "-A"], cwd=sample_repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "profile"],
                   cwd=sample_repo, check=True)
    st = Store(cfg.db_path)
    ids = []
    for _ in range(2):
        fake = _provider()
        o = Orchestrator(cfg, st, echo=None, llm_transport=fake.transport)
        ids.append(await o.execute(issue_file=str(ISSUE), repo=str(sample_repo)))
        assert o.sandbox.agent_env == {"PYTEST_ADDOPTS": "-p no:randomly"}
    first, second = ids
    assert st.get_run(second)["state"] == "COMPLETED"
    b1 = [t for t in st.test_runs(first) if t["label"] == "baseline"][0]
    b2 = [t for t in st.test_runs(second) if t["label"] == "baseline"][0]
    assert b1["duration_s"] > 0 and b2["duration_s"] == 0 and b2["passed"] == b1["passed"]
    assert any("baseline reused from run " + first in e["message"] for e in st.events(second))
