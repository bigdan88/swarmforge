"""Decision explanations and bounded recovery approvals; no live models or repositories."""
import hashlib
import time
import asyncio

import pytest

from swarmforge.execution import ensure_allowance
from swarmforge.projects import service
from swarmforge.projects.recovery import recovery_state
from swarmforge.projects.spec import ProjectError
from swarmforge.services.dashboard import DashboardActions
from swarmforge.webapp.workspace import WorkspaceReader
from .test_project_recovery import state, finish
from .test_projects import Repo, proposal


def stopped_task(store, ps, pid, cfg):
    task = store.queue_add("Polish CSS", "Improve styling", "owner/inventory", budget=1, project_id=pid,
                           milestone=0, project_key="polish", queue=service.lane(pid))
    store.create_run("tested", budget_usd=1)
    eid = ensure_allowance(store, "tested", cfg)
    store._exec("INSERT INTO acceptance(run_id,cycle,criterion_id,text,status,evidence) VALUES ('tested',1,'AC1','CSS renders','satisfied','test_css passed')")
    store._exec("INSERT INTO acceptance(run_id,cycle,criterion_id,text,status,evidence) VALUES ('tested',1,'AC2','Browser check must remain unverified','unverified','no tests mapped')")
    store.create_run("budget-stop", execution_id=eid)
    store.update_run("budget-stop", state="BUDGET_EXCEEDED", state_reason="Remaining task allowance cannot cover the next model request", stop_kind="allowance_exhausted")
    store.queue_update(task, status="failed", run_id="budget-stop", execution_id=eid, project_processed=1)
    store._exec("UPDATE execution_allowances SET spent_usd=.75,deadline=? WHERE id=?", (time.time() - 60, eid))
    store.record_llm_call("tested", cost_usd=.75, cost_source="estimated", status="ok")
    service.stop(ps, ps.get(pid), "Task execution stopped: review its retained evidence and allowance before continuing.", {"run_id": "budget-stop", "task": "polish"})
    return task, eid


def approval(store, ps, pid):
    p = ps.get(pid)
    return {"expected_spec": hashlib.sha256(p["spec"].encode()).hexdigest(),
            "expected_recovery": recovery_state(store, p)["decision_token"], "extra_budget": 1, "hours": 2,
            "spending_authorized": True}


def test_budget_stop_explains_last_tested_attempt_instead_of_claiming_all_checks_failed(state, cfg):
    store, ps, pid = state
    task, eid = stopped_task(store, ps, pid, cfg)
    recovery = WorkspaceReader(store.path).project(pid)["recovery"]
    assert recovery["can_extend_task"]
    assert recovery["decision"]["task"]["id"] == task
    assert recovery["decision"]["passed_checks"] == 1
    assert recovery["decision"]["checks"] == [{"text": "Browser check must remain unverified", "status": "unverified", "explanation": "no tests mapped"}]
    assert recovery["decision_token"] == recovery_state(store, ps.get(pid))["decision_token"]
    assert "owner_token" not in str(recovery)


def test_plain_continue_cannot_reset_an_exhausted_task_allowance(state, cfg):
    store, ps, pid = state
    stopped_task(store, ps, pid, cfg)
    with pytest.raises(ProjectError, match="bounded allowance extension"):
        service.resume(store, pid, "continue")
    assert ps.get(pid)["status"] == "waiting_for_you"


def test_configured_build_floor_is_applied_to_new_work_and_keeps_the_raw_proposal(state, cfg):
    store, ps, pid = state
    cfg.projects.min_build_budget_usd = 2
    async def model(*args):
        assert args[4]["min_build_budget_usd"] == 2
        return proposal()  # model proposes $1 builds
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert all(q["budget"] == 2 for q in ps.items(pid))
    saved = ps.decisions(pid)[0]
    assert saved["proposal"]["tasks"][0]["budget_usd"] == 1
    assert ps.get(pid)["budget_usd"] == 30
    assert any(e["kind"] == "proposal_normalized" and "$2" in e["detail"] for e in ps.events(pid))


async def test_worker_restart_waits_for_the_active_planner_instead_of_cancelling_it(state, cfg, monkeypatch):
    from swarmforge import runqueue
    store, ps, pid = state
    steps = []
    async def planning(*args, **kwargs):
        steps.append("planning")
        store.kv_set(runqueue.RESTART_KEY, "1")
        await asyncio.sleep(.03)
        steps.append("finished")
    def restart(*args, **kwargs):
        assert steps == ["planning", "finished"]
        steps.append("restarted")
    monkeypatch.setattr(service, "tick", planning)
    monkeypatch.setattr(runqueue, "start_worker", restart)
    monkeypatch.setattr(runqueue, "worker_pid", lambda *_: None)
    await runqueue.work(cfg, store, echo=None, poll_s=.005)
    assert steps == ["planning", "finished", "restarted"]
    assert ps.get(pid)["status"] == "running"


def test_extension_is_explicit_idempotent_and_retains_costs_and_lineage_when_replanning(state, cfg):
    store, ps, pid = state
    task, eid = stopped_task(store, ps, pid, cfg)
    args = approval(store, ps, pid)
    actions = DashboardActions(store, cfg, worker_start=lambda *_: None, reconcile_on_start=False)
    assert not actions.execute("no-consent-0000001", "extend_project_task", pid, {**args, "spending_authorized": False})["ok"]
    result = actions.execute("extension-00000001", "extend_project_task", pid, args)
    assert result["ok"], result
    assert actions.execute("extension-00000001", "extend_project_task", pid, args) == result
    row = store._one("SELECT * FROM execution_allowances WHERE id=?", (eid,))
    assert row["budget_usd"] == 2 and row["spent_usd"] == .75 and row["deadline"] > time.time() + 7100
    assert ps.spent(pid) == .75 and ps.get(pid)["budget_usd"] == 30
    assert store.queue_item(task)["status"] == "failed"  # retained history
    async def model(*args):
        return proposal()
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    pending = [q for q in ps.items(pid) if q["status"] in ("queued", "tentative")]
    assert pending and all(q["execution_id"] == eid for q in pending)
    assert not store.kv_get("project_recovery:" + pid)


@pytest.mark.parametrize("change", ["stale", "cap", "cancelled", "uncertain", "foreign", "active", "external"])
def test_extension_rejects_changed_or_unsafe_approvals_without_spending_reset(state, cfg, change):
    store, ps, pid = state
    task, eid = stopped_task(store, ps, pid, cfg)
    args = approval(store, ps, pid)
    if change == "stale":
        ps.event(pid, "new_information", {})
    elif change == "cap":
        args["extra_budget"] = 25
    elif change == "cancelled":
        store._exec("UPDATE execution_allowances SET cancelled=1 WHERE id=?", (eid,))
    elif change == "uncertain":
        store._exec("INSERT INTO execution_calls VALUES ('pending',?,'budget-stop',.2,NULL,'reserved')", (eid,))
    elif change == "foreign":
        store.queue_update(task, milestone=1)
    elif change == "active":
        store._exec("UPDATE execution_allowances SET active_run='someone' WHERE id=?", (eid,))
    elif change == "external":
        store._exec("INSERT INTO execution_checkpoints VALUES (?,'external:publish','budget-stop',?,'uncertain','{}')", (eid, time.time()))
    with pytest.raises(ProjectError):
        service.extend_task_allowance(cfg, store, pid, **{k: v for k, v in args.items() if k != "spending_authorized"})
    row = store._one("SELECT * FROM execution_allowances WHERE id=?", (eid,))
    assert row["budget_usd"] == 1 and row["spent_usd"] == .75
    assert ps.get(pid)["status"] == "waiting_for_you"


def test_checkpoint_lists_actual_checks_and_correction_does_not_accept_them(state):
    store, ps, pid = state
    service.checkpoint(store, ps.get(pid), "Merged docs; physical recording is still needed.")
    p = ps.get(pid)
    recovery = recovery_state(store, p)
    assert recovery["decision"]["kind"] == "checkpoint"
    assert len(recovery["decision"]["checks"]) == 2
    assert "not a task crash" in recovery["decision"]["summary"]
    with pytest.raises(ProjectError, match="Confirm the checks"):
        service.resume(store, pid, "continue", expected_recovery=recovery["decision_token"])
    service.resume(store, pid, "Inspect the tests and repair missing evidence automatically.", expected_recovery=recovery["decision_token"])
    assert ps.get(pid)["current_milestone"] == 0
    assert ps.milestones(pid)[0]["status"] != "done"
    with pytest.raises(ProjectError):
        service.resume(store, pid, "continue", confirm_manual=True, expected_recovery=recovery["decision_token"])


def test_verified_milestone_can_advance_automatically_but_never_fabricates_owner_approval(state, monkeypatch):
    store, ps, pid = state
    p = ps.get(pid)
    p["definition"]["ask_before"] = "None"
    checks = [{"text": text, "status": "passed", "evidence": [{"run": "merged", "test": "pytest"}]} for text in p["definition"]["milestones"][0]["checks"]]
    monkeypatch.setattr(service, "verify_checks", lambda *_: checks)
    before = store.conversation_messages(p["conversation_id"])
    service.checkpoint(store, p, "Recorded evidence verifies all checks.")
    assert ps.get(pid)["status"] == "running" and ps.get(pid)["current_milestone"] == 1
    assert ps.milestones(pid)[0]["status"] == "done"
    after = store.conversation_messages(p["conversation_id"])[len(before):]
    assert all(m["role"] == "assistant" for m in after)
    assert any(e["kind"] == "milestone_auto_accepted" for e in ps.events(pid))
