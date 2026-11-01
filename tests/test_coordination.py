"""Several runs on one repository, the way a human team works: a manager checks the tasks are different
enough, each run works on its own branch, and the branch is updated with the latest base before its PR."""
import asyncio
import json
import os
import subprocess
import time

import pytest

from swarmforge.db import Store
from swarmforge.github import Issue
from swarmforge.llm import Budget, LLMClient
from swarmforge.orchestrator import Orchestrator, RunAborted
from swarmforge.plan import Plan, Task

from .conftest import ISSUE, FakeProvider


def _orch(cfg, st, answer, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    seen = []

    def planner(body):
        seen.append(body)
        return {"content": json.dumps(answer)}
    fake = FakeProvider({"qwen-fake": planner})
    st.create_run("new", repo="bigdan88/app", pid=os.getpid())
    o = Orchestrator(cfg, st, echo=None)
    o.run_id, o.ws, o._repo_poll_s, o._started = "new", None, 0.05, time.monotonic()
    o._repo_arg, o._issue_url_arg, o._git_auth = "bigdan88/app", None, []
    o.llm = LLMClient(o.cfg, st, "new", Budget(5), transport=fake.transport)
    o.issue = Issue(title="Dark mode", body="Add a dark theme to the dashboard.")
    return o, seen


def _peer(st, rid, plan_files=None, title="Filter runs"):
    plan = {"summary": "s", "tasks": [{"id": "T1", "title": "t", "expected_files": plan_files}]} if plan_files else None
    st.create_run(rid, repo="https://github.com/bigdan88/app.git", state="TASK_EXECUTION", pid=os.getpid(),
                  issue_title=title, issue_body="Filters above the runs table.",
                  plan_json=json.dumps(plan) if plan else None)
    time.sleep(0.01)


async def test_manager_lets_different_work_run_in_parallel(cfg, tmp_path, monkeypatch):
    st = Store(tmp_path / "db")
    _peer(st, "peer", ["src/app/filters.py"])
    st.update_run("peer", created_at=time.time() - 60)
    o, seen = _orch(cfg, st, {"decision": "parallel", "overlaps_with": [], "reason": "different areas"}, monkeypatch)
    await asyncio.wait_for(o._coordinate("start"), 2)
    prompt = seen[0]["messages"][1]["content"]
    assert "Dark mode" in prompt and "Run peer" in prompt and "filters.py" in prompt
    assert any("manager: parallel" in e["message"] for e in st.events("new"))


async def test_manager_waits_for_overlapping_work_and_blocks_duplicates(cfg, tmp_path, monkeypatch):
    st = Store(tmp_path / "db")
    _peer(st, "peer")
    st.update_run("peer", created_at=time.time() - 60)
    o, _ = _orch(cfg, st, {"decision": "wait", "overlaps_with": ["peer"], "reason": "same module"}, monkeypatch)
    waiter = asyncio.ensure_future(o._coordinate("start"))
    await asyncio.sleep(0.3)
    assert not waiter.done()
    assert st.get_run("new")["state_reason"].startswith("waiting for the repository: run(s) peer — same module")
    st.update_run("peer", state="COMPLETED")
    await asyncio.wait_for(waiter, 3)
    assert st.get_run("new")["state_reason"] is None

    st2 = Store(tmp_path / "db2")
    _peer(st2, "peer")
    st2.update_run("peer", created_at=time.time() - 60)
    o2, _ = _orch(cfg, st2, {"decision": "duplicate", "overlaps_with": ["peer"], "reason": "same feature"},
                  monkeypatch)
    with pytest.raises(RunAborted, match="duplicate of run"):
        await o2._coordinate("start")


async def test_after_planning_runs_touching_the_same_files_take_turns(cfg, tmp_path, monkeypatch):
    st = Store(tmp_path / "db")
    _peer(st, "peer", ["src/app/dashboard.py", "README.md"])
    st.update_run("peer", created_at=time.time() - 60)
    o, _ = _orch(cfg, st, {}, monkeypatch)
    o.plan = Plan(summary="s", tasks=[Task(id="T1", title="t", role="developer_backend", description="d",
                                           expected_files=["./src/app/dashboard.py", "src/app/theme.py"])],
                  acceptance_criteria=[])
    waiter = asyncio.ensure_future(o._coordinate("plan"))
    await asyncio.sleep(0.3)
    assert not waiter.done() and "src/app/dashboard.py" in st.get_run("new")["state_reason"]
    st.update_run("peer", state="FAILED")
    await asyncio.wait_for(waiter, 3)
    # no overlap -> carries on immediately
    o.plan.tasks[0].expected_files = ["src/app/theme.py"]
    st.update_run("peer", state="TASK_EXECUTION")
    await asyncio.wait_for(o._coordinate("plan"), 1)
    assert "no file overlap" in st.events("new")[-1]["message"]
    assert "Other work is in progress" in o._peer_note() and "dashboard.py" in o._peer_note()


@pytest.mark.slow
async def test_branch_is_updated_with_the_latest_base_before_delivery(cfg, sample_repo, monkeypatch):
    """main moves on while the run works (a teammate's PR was merged): the branch gets the new commit,
    the tests run again, and the delivered branch contains both."""
    from .test_e2e import _provider
    st = Store(cfg.db_path)
    orig = Orchestrator._review
    moved = {}

    async def review_then_teammate_merges(self, cycle, ev):
        if not moved:
            (sample_repo / "CONTRIBUTING.md").write_text("Be nice.\n")
            subprocess.run(["git", "add", "-A"], cwd=sample_repo, check=True)
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "teammate"],
                           cwd=sample_repo, check=True)
            moved["sha"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=sample_repo, capture_output=True,
                                          text=True).stdout.strip()
        return await orig(self, cycle, ev)
    monkeypatch.setattr(Orchestrator, "_review", review_then_teammate_merges)
    run_id = await Orchestrator(cfg, st, echo=None, llm_transport=_provider().transport).execute(
        issue_file=str(ISSUE), repo=str(sample_repo))
    run = st.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    assert any("moved on" in e["message"] for e in st.events(run_id))
    assert any(t["label"].startswith("update") and t["exit_code"] == 0 for t in st.test_runs(run_id))
    branch = run["state_reason"].split("delivered to branch ")[1].split(" in ")[0]
    rc = subprocess.run(["git", "merge-base", "--is-ancestor", moved["sha"], branch], cwd=sample_repo).returncode
    assert rc == 0                                         # the delivered branch contains the teammate's work
    files = subprocess.run(["git", "diff", "--name-only", f"{moved['sha']}...{branch}"], cwd=sample_repo,
                           capture_output=True, text=True).stdout
    assert "CONTRIBUTING.md" not in files and "inventory/exporters.py" in files   # PR diff = only this run's work
