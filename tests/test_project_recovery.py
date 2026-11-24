"""Automatic planning repair and human recovery use fake models/repositories only."""
import asyncio
import copy
import hashlib
import json

import pytest

from swarmforge.db import Store
from swarmforge.projects import service
from swarmforge.projects.actions import LeadAction, existing_task_reference, validate_scope
from swarmforge.projects.recovery import recovery_state
from swarmforge.projects.spec import ProjectError, fixed
from swarmforge.projects.store import Projects
from swarmforge.services.dashboard import DashboardActions
from swarmforge.webapp.workspace import WorkspaceReader
from .test_projects import Repo, definition, proposal


def finish(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError("A fake adapter unexpectedly suspended")


@pytest.fixture
def state(cfg, monkeypatch):
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)
    async def wait(awaitable, timeout):
        return await awaitable
    monkeypatch.setattr(asyncio, "to_thread", direct)
    monkeypatch.setattr(asyncio, "wait_for", wait)
    store = Store(cfg.db_path)
    ps = Projects(store)
    project = ps.create()
    ps.draft(project["id"], definition())
    service.approve(store, project["id"])
    yield store, ps, project["id"]
    store.close()


def accepted_horizon(store, pid):
    first = store.queue_add("Research recorder", "Existing approved research scope", "owner/inventory", mode="single", budget=1.5,
                            project_id=pid, milestone=0, project_key="spike:probe", queue=service.lane(pid), max_runtime_s=120)
    second = store.queue_add("Document findings", "Existing approved documentation scope", "owner/inventory", budget=1,
                             project_id=pid, milestone=0, project_key="docs", queue=service.lane(pid), depends_on=json.dumps(["spike:probe"]))
    return first, second


def decision(kind, **values):
    return {"action": kind, "reason": "Continue within the approved specification", "evidence": ["repository:main-1"], **values}


def test_existing_investigation_reference_continues_without_replacing_future_tasks(state, cfg):
    store, ps, pid = state
    first, second = accepted_horizon(store, pid)
    before = copy.deepcopy(ps.items(pid))
    spec = ps.get(pid)["spec"]
    async def model(*args):
        return decision("investigate", task="probe", hypothesis="Compare supported capture paths", observable_result="Retain findings",
                        budget_usd=1.5, max_runtime_s=120)
    repo = Repo()
    finish(service.advance(cfg, store, pid, repository=repo, ask=model))
    assert ps.items(pid) == before
    assert ps.get(pid)["spec"] == spec and "write" not in repo.touched
    assert service.can_start(store, store.queue_item(first))
    assert not service.can_start(store, store.queue_item(second))
    assert ps.decisions(pid)[0]["status"] == "applied"
    assert ps.decisions(pid)[0]["result"]["normalization"]
    assert any(e["kind"] == "plan_continued" for e in ps.events(pid))


def test_adapter_never_discards_permission_changes_or_different_allowances(state):
    store, ps, pid = state
    accepted_horizon(store, pid)
    project, items = ps.get(pid), ps.items(pid)
    for fields in ({"permissions": True}, {"budget_usd": 20}, {"budget_usd": .5}, {"max_runtime_s": 999}, {"task": "foreign"}):
        raw = decision("investigate", task="probe") | fields
        value, note = existing_task_reference(raw, project, items)
        assert value == raw and note is None


def test_continue_cannot_skip_dependencies_or_tentative_or_foreign_work(state, cfg):
    store, ps, pid = state
    first, second = accepted_horizon(store, pid)
    p = ps.get(pid)
    for target in (second, first + 999):
        with pytest.raises(ProjectError, match="next undispatched"):
            validate_scope(LeadAction.parse(decision("continue_plan", task_id=target), "repository:main-1"), p, ps.items(pid),
                           {"repository:main-1": {}}, max_runtime_s=300, investigation_timeout_s=900)
    store.queue_update(first, depends_on='["not-completed"]')
    with pytest.raises(ProjectError, match="unfinished prerequisites"):
        validate_scope(LeadAction.parse(decision("continue_plan", task_id=first), "repository:main-1"), p, ps.items(pid),
                       {"repository:main-1": {}}, max_runtime_s=300, investigation_timeout_s=900)


def test_bad_json_is_repaired_with_specific_feedback_and_keeps_recorded_costs(state, cfg):
    store, ps, pid = state
    calls = []
    original = fixed(ps.get(pid)["spec"])
    async def model(*args):
        context = args[4]
        calls.append(copy.deepcopy(context))
        store.record_llm_call("project:" + pid, task_key="project-lead:m0", cost_usd=.01, cost_source="estimated", status="ok")
        if len(calls) < 3:
            raise ProjectError("No JSON object found")
        assert "JSON" in context["correction_required"] and "action_examples" in context
        return proposal()
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert len(calls) == 3 and ps.get(pid)["status"] == "running"
    assert ps.spent(pid) == pytest.approx(.03)
    assert fixed(ps.get(pid)["spec"]) == original
    assert [d["status"] for d in reversed(ps.decisions(pid))] == ["rejected", "rejected", "applied"]


def test_exhausted_repairs_have_explanations_and_guarded_retriable_state(state, cfg):
    store, ps, pid = state
    async def invalid(*args):
        return decision("investigate", task="unknown", hypothesis="Needs a valid task object")
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=invalid))
    p = ps.get(pid)
    recovery = recovery_state(store, p)
    assert p["status"] == "waiting_for_you" and recovery["can_retry"]
    assert recovery["attempts"] == cfg.projects.max_lead_actions
    assert "hypothesis" in recovery["rejections"][0]["error"]
    assert "model configuration" not in p["last_error"]
    public = WorkspaceReader(store.path).project(pid)
    assert public["recovery"]["can_retry"] and public["recovery"]["rejections"]
    digest = hashlib.sha256(p["spec"].encode()).hexdigest()
    with pytest.raises(ProjectError, match="changed"):
        service.retry_planning(store, pid, expected_spec=digest, expected_decision=-1)
    worker = []
    actions = DashboardActions(store, cfg, worker_start=lambda *_: worker.append(1), reconcile_on_start=False)
    result = actions.execute("retry-planning-000001", "retry_project_lead", pid,
                             {"expected_spec": digest, "expected_decision": recovery["latest_decision_id"], "spending_authorized": True})
    assert result["ok"] and worker == [1]
    assert ps.get(pid)["status"] == "running" and ps.get(pid)["spec"] == p["spec"]
    assert len(ps.decisions(pid)) == cfg.projects.max_lead_actions  # rejection history is retained


def test_uncertain_action_cannot_use_planning_retry(state):
    store, ps, pid = state
    service.stop(ps, ps.get(pid), "Project lead worker was lost during a repository write", {})
    p = ps.get(pid)
    assert not recovery_state(store, p)["can_retry"]
    with pytest.raises(ProjectError, match="uncertain action"):
        service.retry_planning(store, pid, expected_spec=hashlib.sha256(p["spec"].encode()).hexdigest(), expected_decision=None)


def test_continuing_existing_horizon_cannot_exceed_milestone_reservation(state, cfg):
    store, ps, pid = state
    first, _ = accepted_horizon(store, pid)
    store.record_llm_call("project:" + pid, task_key="project-lead:m0", cost_usd=19.5, cost_source="estimated", status="ok")
    async def model(*args):
        return decision("continue_plan", task_id=first)
    finish(service.advance(cfg, store, pid, repository=Repo(), ask=model))
    assert ps.get(pid)["status"] == "waiting_for_you"
    assert ps.get(pid)["observed_sha"] is None and "budget" in ps.get(pid)["last_error"]
    assert store.queue_item(first)["budget"] == 1.5


def test_scope_editor_requires_review_and_preserves_repository_and_completed_milestones(state, cfg):
    store, ps, pid = state
    service.pause(store, pid)
    p = ps.get(pid)
    revised = copy.deepcopy(p["definition"])
    revised["goal"] = "Track inventory and audited edits"
    actions = DashboardActions(store, cfg, worker_start=lambda *_: None, reconcile_on_start=False)
    args = {"definition": revised, "expected_spec": hashlib.sha256(p["spec"].encode()).hexdigest()}
    assert not actions.execute("amend-without-ok-001", "amend_project", pid, args)["ok"]
    assert actions.execute("amend-approved-0001", "amend_project", pid, {**args, "spending_authorized": True})["ok"]
    updated = ps.get(pid)
    assert updated["status"] == "waiting_for_you" and updated["previous_spec"] == p["spec"]
    assert updated["repo"] == p["repo"] and updated["definition"]["milestones"] == p["definition"]["milestones"]
    with pytest.raises(ProjectError, match="changed"):
        service.resume(store, pid, expected_spec=args["expected_spec"])
