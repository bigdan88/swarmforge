"""Unattended recovery with fake models/repositories, retaining costs and real verification."""
import asyncio
import copy
import json
import time

import pytest

from swarmforge.projects import autonomy, service
from swarmforge.projects.actions import LeadAction, validate_scope
from swarmforge.projects.recovery import recovery_state
from swarmforge.projects.spec import ProjectError, fixed
from swarmforge.webapp.workspace import WorkspaceReader
from .test_project_recovery import state, finish, decision
from .test_project_decisions import stopped_task
from .test_projects import Repo, proposal


@pytest.fixture
def unattended(state, cfg):
    cfg.projects.autonomous = True
    cfg.projects.auto_retry_delay_s = 0
    return state


def test_tentative_future_dependencies_are_valid_but_never_dispatch(unattended, cfg):
    store, ps, pid = unattended
    plan = proposal()
    plan["tasks"][1].update(ready=False, depends_on=["0"])
    plan["tasks"][2].update(ready=False, depends_on=["1"])
    async def model(*args):
        return plan
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    items = ps.items(pid)
    assert len(items) == 3 and not any(d["status"] == "rejected" for d in ps.decisions(pid))
    assert service.can_start(store, items[0])
    assert not service.can_start(store, items[1]) and not service.can_start(store, items[2])


def test_out_of_order_ready_tasks_are_sorted_without_changing_the_proposed_work(unattended, cfg):
    store, ps, pid = unattended
    plan = proposal()
    plan["tasks"][1]["depends_on"] = ["0"]
    plan["tasks"] = [plan["tasks"][1], plan["tasks"][0], plan["tasks"][2]]
    async def model(*args):
        return plan
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert [q["project_key"] for q in ps.items(pid)] == ["0", "1", "2"]
    assert ps.decisions(pid)[0]["proposal"]["tasks"][0]["key"] == "1"
    assert any(e["kind"] == "proposal_normalized" for e in ps.events(pid))


@pytest.mark.parametrize("bad", ["cycle", "foreign", "ready_on_tentative"])
def test_relaxed_future_graph_never_allows_cycles_foreign_or_premature_work(unattended, cfg, bad):
    store, ps, pid = unattended
    tasks = proposal()["tasks"]
    if bad == "cycle":
        tasks[1].update(ready=False, depends_on=["2"])
        tasks[2]["depends_on"] = ["1"]
    elif bad == "foreign":
        tasks[2]["depends_on"] = ["foreign-project-task"]
    else:
        tasks[0]["depends_on"] = ["2"]
    action = LeadAction.parse(decision("plan", tasks=tasks), "repository:main-1")
    with pytest.raises(ProjectError):
        validate_scope(action, ps.get(pid), [], {"repository:main-1": {}}, max_runtime_s=300, investigation_timeout_s=300)


def test_internal_planning_repair_uses_another_bounded_round_without_owner_messages(unattended, cfg):
    store, ps, pid = unattended
    original = fixed(ps.get(pid)["spec"])
    calls = []
    async def model(*args):
        calls.append(copy.deepcopy(args[4]))
        store.record_llm_call("project:" + pid, task_key="project-lead:m0", cost_usd=.01, cost_source="estimated", status="ok")
        return {"action": "nonsense"} if len(calls) <= cfg.projects.max_lead_actions else proposal()
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert ps.get(pid)["status"] == "running" and not ps.items(pid)
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert ps.items(pid) and ps.spent(pid) == pytest.approx(.04)
    assert calls[-1]["automatic_recovery"]["problem"] == "planning"
    assert fixed(ps.get(pid)["spec"]) == original
    assert not any(m["role"] == "user" for m in store.conversation_messages(ps.get(pid)["conversation_id"]))


def test_invalid_planning_cannot_create_unlimited_new_rounds_even_after_worker_restart(unattended, cfg):
    store, ps, pid = unattended
    calls = []
    async def invalid(*args):
        calls.append(1)
        return {"action": "nonsense"}
    for _ in range(cfg.projects.auto_recovery_rounds + 1):
        finish(service.advance(cfg, store, pid, repository=Repo(), ask=invalid))
    assert len(calls) == cfg.projects.max_lead_actions * (cfg.projects.auto_recovery_rounds + 1)
    assert ps.get(pid)["status"] == "waiting_for_you"
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))
    assert not ps.items(pid)


def test_allowance_recovery_doubles_once_per_failed_attempt_and_keeps_the_lineage(unattended, cfg):
    store, ps, pid = unattended
    task_id, eid = stopped_task(store, ps, pid, cfg)
    assert autonomy.recover_waiting(cfg, store, ps.get(pid))
    row = store._one("SELECT * FROM execution_allowances WHERE id=?", (eid,))
    assert row["budget_usd"] == 2 and row["spent_usd"] == .75
    assert not autonomy.recover_allowance(cfg, store, ps.get(pid), store.queue_item(task_id), store.get_run("budget-stop"))
    async def model(*args):
        return proposal()
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert all(q["execution_id"] == eid for q in ps.items(pid) if q["status"] in ("queued", "tentative"))
    assert ps.spent(pid) == .75 and ps.get(pid)["budget_usd"] == 30
    assert store.queue_item(task_id)["status"] == "failed"


@pytest.mark.parametrize("block", ["cap", "cancelled", "external", "active", "reserved"])
def test_automatic_allowance_recovery_cannot_bypass_actual_blockers(unattended, cfg, block):
    store, ps, pid = unattended
    _, eid = stopped_task(store, ps, pid, cfg)
    if block == "cap":
        store.record_llm_call("project:" + pid, task_key="project-lead:m0", cost_usd=29, cost_source="estimated", status="ok")
    elif block == "cancelled":
        store._exec("UPDATE execution_allowances SET cancelled=1 WHERE id=?", (eid,))
    elif block == "external":
        store._exec("INSERT INTO execution_checkpoints VALUES (?,'external:publish','budget-stop',?,'uncertain','{}')", (eid, time.time()))
    elif block == "active":
        store._exec("UPDATE execution_allowances SET active_run='another' WHERE id=?", (eid,))
    else:
        store._exec("INSERT INTO execution_calls VALUES ('pending',?,'budget-stop',.2,NULL,'reserved')", (eid,))
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))
    assert store._one("SELECT budget_usd,spent_usd FROM execution_allowances WHERE id=?", (eid,)) == {"budget_usd": 1, "spent_usd": .75}
    assert ps.get(pid)["status"] == "waiting_for_you"


def test_auto_recovery_is_bounded_across_distinct_failed_attempts(unattended, cfg):
    store, ps, pid = unattended
    task_id, eid = stopped_task(store, ps, pid, cfg)
    for attempt in range(3):
        if attempt:
            run = f"later-{attempt}"
            store.create_run(run, execution_id=eid)
            store.update_run(run, state="BUDGET_EXCEEDED", stop_kind="allowance_exhausted", state_reason="The next call does not fit")
            store.queue_update(task_id, run_id=run)
            service.stop(ps, ps.get(pid), "Task stopped", {"run_id": run})
        assert autonomy.recover_waiting(cfg, store, ps.get(pid)) is (attempt < 2)
    assert store._one("SELECT budget_usd FROM execution_allowances WHERE id=?", (eid,))["budget_usd"] == 4
    assert ps.get(pid)["status"] == "waiting_for_you"


def test_unverified_device_checks_are_deferred_to_final_review_without_pretending_they_passed(unattended, cfg, monkeypatch):
    store, ps, pid = unattended
    checks = [{"text": "Real device recording plays", "status": "needs you", "evidence": []}]
    monkeypatch.setattr(service, "verify_checks", lambda *_: copy.deepcopy(checks))
    service.checkpoint(store, ps.get(pid), "Software ready; physical device unavailable", config=cfg)
    assert ps.get(pid)["status"] == "running" and ps.get(pid)["current_milestone"] == 0  # verify automatically first
    service.checkpoint(store, ps.get(pid), "The owner device is still unavailable", config=cfg)
    assert ps.get(pid)["current_milestone"] == 1 and ps.milestones(pid)[0]["status"] == "verification_pending"
    assert json.loads(ps.milestones(pid)[0]["report"])["checks"][0]["status"] == "needs you"
    checks[0].update(text="Software integration tests pass", status="passed", evidence=[{"test": "integration", "run": "merged"}])
    service.checkpoint(store, ps.get(pid), "Milestone 2 verified", config=cfg)
    assert ps.get(pid)["current_milestone"] == 2 and ps.get(pid)["status"] == "running"
    service.checkpoint(store, ps.get(pid), "Final software milestone verified", config=cfg)
    p = ps.get(pid)
    assert p["status"] == "at_checkpoint"  # final handoff, not fake completion
    report = json.loads(ps.milestones(pid)[2]["report"])
    assert report["final_review"] and report["pending_milestones"][0]["checks"][0]["status"] == "needs you"
    public = WorkspaceReader(store.path).project(pid)
    assert public["recovery"]["decision"]["final_review"]
    assert any("Real device recording" in c["text"] for c in public["recovery"]["decision"]["checks"])
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))  # final review never loops automatically
    with pytest.raises(ProjectError, match="Confirm"):
        service.resume(store, pid, "continue")
    service.resume(store, pid, "continue", confirm_manual=True)
    assert ps.get(pid)["status"] == "done" and all(m["status"] == "done" for m in ps.milestones(pid))


def test_intentional_pause_draft_and_uncertain_repository_write_are_never_auto_resumed(unattended, cfg):
    store, ps, pid = unattended
    service.pause(store, pid)
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))
    ps.update(pid, status="running")
    service.stop(ps, ps.get(pid), "Project lead worker was lost during a repository write", {})
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))
    draft = ps.create()
    assert not autonomy.recover_waiting(cfg, store, draft)


def test_resolved_rejection_is_labelled_as_history(unattended, cfg):
    store, ps, pid = unattended
    calls = []
    async def model(*args):
        calls.append(1)
        return {"action": "nonsense"} if len(calls) == 1 else proposal()
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    recovery = recovery_state(store, ps.get(pid))
    assert recovery["rejections"][0]["resolved"]
    assert recovery["rejections"][0]["resolved_by"] == ps.decisions(pid)[0]["id"]


def test_safe_planner_cancellation_recovers_but_uncertain_write_remains_blocked(unattended, cfg):
    store, ps, pid = unattended
    async def cancelled(*args):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        finish(service.advance(cfg, store, pid, repository=Repo(), ask=cancelled))
    assert ps.get(pid)["status"] == "running"
    assert any(e["kind"] == "automatic_recovery" for e in ps.events(pid))
    async def model(*args):
        return proposal()
    class InterruptedWrite(Repo):
        def write_spec(self, *args):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        finish(service.advance(cfg, store, pid, repository=InterruptedWrite(), ask=model))
    assert ps.get(pid)["status"] == "waiting_for_you"


def test_questions_get_an_automatic_alternative_before_interrupting_the_owner(unattended, cfg):
    store, ps, pid = unattended
    calls = []
    async def model(*args):
        calls.append(1)
        return decision("ask_user", question="Which sample data should I use?") if len(calls) == 1 else proposal()
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert ps.get(pid)["status"] == "running"
    assert not any(e["kind"] == "escalation" for e in ps.events(pid))
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert ps.items(pid)


def test_unattended_delivery_can_reallocate_milestone_estimates_but_not_the_project_cap(unattended, cfg):
    store, ps, pid = unattended
    store.record_llm_call("project:" + pid, task_key="project-lead:m0", cost_usd=21, cost_source="estimated", status="ok")
    async def model(*args):
        return proposal()
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert ps.get(pid)["status"] == "running" and ps.items(pid)
    assert any(e["kind"] == "budget_reallocated" for e in ps.events(pid))
    store.record_llm_call("project:" + pid, task_key="project-lead:m0", cost_usd=9, cost_source="estimated", status="ok")
    ps.update(pid, observed_sha=None)
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert ps.get(pid)["status"] == "waiting_for_you" and ps.spent(pid) == 30


def test_environment_recheck_is_bounded_and_cannot_override_an_owner_pause(unattended, cfg):
    from swarmforge import runqueue
    store, ps, pid = unattended
    name = service.lane(pid)
    task = store.queue_add("Retained task", "body", "owner/inventory", project_id=pid, milestone=0, project_key="work", queue=name)
    store.queue_update(task, note=runqueue.ENV_PAUSE_NOTE + ": Docker is not running")
    runqueue.set_lane_paused(store, name, True)  # owner pause
    autonomy.recover_environment(cfg, store, ps.get(pid))
    assert runqueue.lane_paused(store, name)
    for attempt in range(3):
        runqueue.set_lane_paused(store, name, True, reason="environment")
        autonomy.recover_environment(cfg, store, ps.get(pid))
        assert runqueue.lane_paused(store, name) is (attempt == 2)
    assert ps.get(pid)["status"] == "waiting_for_you"
    assert store.queue_item(task)["status"] == "queued"


def test_failed_attempt_reuses_retained_work_and_its_failure_reason(unattended, cfg, monkeypatch):
    store, ps, pid = unattended
    q = store.queue_add("Build", "body", "owner/inventory", budget=2, project_id=pid, milestone=0, project_key="build", queue=service.lane(pid))
    store.create_run("retained")
    store.update_run("retained", state="FAILED", stop_kind="task_failure", state_reason="Regression test test_import failed")
    store.queue_update(q, status="failed", run_id="retained")
    monkeypatch.setattr("swarmforge.postmortem.resumable", lambda *_: True)
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=None))
    retry = ps.items(pid)[-1]
    assert retry["seed_run_id"] == "retained" and retry["retry_of"] == "retained"
    assert "Regression test test_import failed" in retry["body"]


async def test_idle_worker_finishes_recovery_even_when_every_project_starts_waiting(unattended, cfg, monkeypatch):
    from swarmforge import runqueue
    store, ps, pid = unattended
    ps.update(pid, status="waiting_for_you", last_error="Internal planning response error")
    finished = []
    async def recover(*args, **kwargs):
        await asyncio.sleep(.02)
        finished.append(True)
        store.kv_set(runqueue.PAUSED_KEY, "1")  # stop this fake worker after its recovery callback
    monkeypatch.setattr(service, "tick", recover)
    monkeypatch.setattr(runqueue, "worker_pid", lambda *_: None)
    await runqueue.work(cfg, store, echo=None, poll_s=.005)
    assert finished == [True]


def test_transient_provider_timeout_keeps_conservative_costs_and_recovers_without_owner_reply(unattended, cfg):
    import httpx
    store, ps, pid = unattended
    async def timeout(*args):
        raise httpx.ReadTimeout("Temporary provider timeout")
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=timeout))
    assert ps.get(pid)["status"] == "running"
    assert ps.spent(pid) == cfg.projects.lead_budget_usd
    async def model(*args):
        return proposal()
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert ps.items(pid) and ps.spent(pid) == cfg.projects.lead_budget_usd


def test_an_unknown_model_reply_already_charged_to_the_task_is_not_a_publication_blocker(unattended, cfg):
    store, ps, pid = unattended
    _, eid = stopped_task(store, ps, pid, cfg)
    store._exec("INSERT INTO execution_calls VALUES ('lost-reply',?,'budget-stop',.2,.2,'uncertain')", (eid,))
    assert autonomy.recover_waiting(cfg, store, ps.get(pid))
    assert store._one("SELECT spent_usd FROM execution_allowances WHERE id=?", (eid,))["spent_usd"] == .75


@pytest.mark.parametrize("owner_action", ["pause", "cancel"])
def test_lost_lead_cleanup_does_not_overwrite_a_later_owner_pause_or_cancellation(unattended, cfg, owner_action):
    from swarmforge.projects.decisions import LeadTurn, epoch, _fail_unfinished
    store, ps, pid = unattended
    turn = LeadTurn.claim(store, pid)
    turn.begin(epoch(store, ps.get(pid)), {"_policy": {}}, 3)
    turn.candidate(proposal(), action="plan", reason="A saved proposal")
    turn.mark("validated")
    getattr(service, owner_action)(store, pid)
    before = ps.get(pid)
    with store.transaction():
        assert _fail_unfinished(store.conn, pid)
    assert ps.get(pid)["status"] == before["status"] and ps.get(pid)["last_error"] == before["last_error"]
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))
    turn.release(failed=True)
