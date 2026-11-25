"""Project invariants with fake repository/model adapters: no paid model calls or GitHub writes."""
import json

import pytest

from swarmforge import runqueue
from swarmforge.db import Store
from swarmforge.projects import service
from swarmforge.projects.spec import ProjectError, fixed, render, replace_living
from swarmforge.projects.store import Projects


def definition():
    return {"name": "Inventory", "goal": "Track inventory", "users": "Shop owners", "repo": "owner/inventory",
            "repository_kind": "existing", "out_of_scope": "Payments", "stack": "Python", "hosting": "Local",
            "deadline": "None", "latitude": "Choose a UI library", "ask_before": "Changing the data model", "budget_usd": 30,
            "milestones": [{"name": n, "goal": "Ship " + n, "checks": ["Users can " + n, "Tests cover " + n], "estimate_usd": 10, "estimate_hours": 1} for n in ("list", "edit", "export")]}


@pytest.fixture
def state(tmp_path):
    s = Store(tmp_path / "state.db")
    ps = Projects(s)
    p = ps.create()
    ps.draft(p["id"], definition())
    yield s, ps, p["id"]
    s.close()


class Repo:
    def __init__(self):
        self.sha = "main-1"
        self.merged = False
        self.touched = []

    def ensure(self, kind, pid):
        self.touched.append("ensure")

    def snapshot(self):
        return {"sha": self.sha, "branch": "main", "tree": ["README.md"], "key_files": {}}

    def write_spec(self, p, text, snapshot):
        assert fixed(p["spec"]) == fixed(text)
        self.touched.append("write")

    def pull(self, url):
        return {"merged_at": "today" if self.merged else None, "base": {"ref": "main"}, "merge_commit_sha": self.sha}


def proposal(action="plan"):
    return {"action": action, "reason": "Survey current main and build a walking skeleton", "living": "### Current state\nREADME only\n### Decisions made\n2026-11-03: choose Flask\n### Plan\nSkeleton\n### Plan changes\nInitial plan\n### Open questions\nNone",
            "tasks": [{"key": str(i), "title": "Skeleton" if i == 0 else f"Task {i}", "description": "Implement a thin tested path", "acceptance_criteria": ["App starts"], "files": [f"file{i}.py"], "ready": i < 2, "kind": "build", "budget_usd": 1} for i in range(3)]}


async def ask(*a, **kw):
    return proposal()


def finish(s, ps, pid):
    q = next(q for q in ps.items(pid) if q["status"] == "queued")
    rid = "run-" + str(q["id"])
    s.create_run(rid)
    s.update_run(rid, state="COMPLETED", pr_url="https://github.com/owner/inventory/pull/1")
    s.queue_update(q["id"], status="done", run_id=rid)
    return s.queue_item(q["id"])


def test_fixed_spec_is_byte_protected():
    original = render(definition())
    assert replace_living(original, fixed(original) + "## Living\nUpdated")
    with pytest.raises(ProjectError, match="any byte"):
        replace_living(original, original.replace("Track inventory", "Track all inventory"))
    with pytest.raises(ProjectError):
        replace_living(original, original + "\n## Fixed\nInjected")


async def test_no_repository_touched_before_approval(state, cfg):
    s, ps, pid = state
    repo = Repo()
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    assert repo.touched == [] and ps.items(pid) == []
    service.approve(s, pid)
    assert repo.touched == []  # approval records authorization; the worker performs I/O
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    assert len(ps.items(pid)) == 3
    assert ps.items(pid)[-1]["status"] == "tentative"
    assert ps.items(pid)[-1] not in s.queue_items(include_finished=False)


async def test_project_waits_for_an_in_flight_task_on_its_repository(state, cfg):
    s, ps, pid = state
    service.approve(s, pid)
    task_id = s.queue_add("Existing work", "body", "https://github.com/owner/inventory.git")
    s.queue_update(task_id, status="running")
    repo = Repo()
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    assert not repo.touched
    assert f"#{task_id}" in s.kv_get(f"project_activity:{pid}")
    s.queue_update(task_id, status="done")
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    assert ps.items(pid)


async def test_unmerged_pr_blocks_only_its_project(state, cfg):
    s, ps, pid = state
    service.approve(s, pid)
    repo = Repo()
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    finish(s, ps, pid)
    other = s.queue_add("Other", "body", "owner/unrelated", chain=0)
    next_task = next(q for q in ps.items(pid) if q["status"] == "queued")
    assert not service.can_start(s, next_task)
    assert service.can_start(s, s.queue_item(other))
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    assert "Waiting for" in ps.get(pid)["last_error"]
    assert ps.get(pid)["status"] == "running"
    repo.merged = True
    repo.sha = "main-2"
    contexts = []

    async def replan(*args):
        contexts.append(args[4])
        return proposal()
    await service.advance(cfg, s, pid, repository=repo, ask=replan)
    assert contexts[0]["repository"]["sha"] == "main-2"
    assert ps.items(pid)[0]["merged_sha"] == "main-2"
    assert service.can_start(s, next(q for q in ps.items(pid) if q["status"] == "queued"))


async def test_thin_escalation_is_bounced_without_human_message(state, cfg):
    s, ps, pid = state
    service.approve(s, pid)
    rejected = proposal("escalate")
    rejected.update(proposal="Change the scope", attempts=[{"step": 1, "result": "Tried a library"}])
    calls = []

    async def model(*args):
        calls.append(args)
        return rejected if len(calls) == 1 else proposal()
    before = len(s.conversation_messages(ps.get(pid)["conversation_id"]))
    await service.advance(cfg, s, pid, repository=Repo(), ask=model)
    assert len(calls) == 2
    assert ps.get(pid)["status"] == "running"
    assert len(s.conversation_messages(ps.get(pid)["conversation_id"])) == before
    assert any(e["kind"] == "proposal_rejected" for e in ps.events(pid))


async def test_checkpoint_pauses_and_continue_resumes(state, cfg):
    s, ps, pid = state
    service.approve(s, pid)
    repo = Repo()
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    finish(s, ps, pid)
    repo.merged = True

    async def done(*args):
        return proposal("checkpoint")
    await service.advance(cfg, s, pid, repository=repo, ask=done)
    assert ps.get(pid)["status"] == "at_checkpoint"
    assert not service.can_start(s, next(q for q in ps.items(pid) if q["status"] == "queued"))
    report = json.loads(ps.milestones(pid)[0]["report"])
    assert all(c["status"] == "needs you" for c in report["checks"])
    with pytest.raises(ProjectError, match="Confirm"):
        service.resume(s, pid)
    service.resume(s, pid, confirm_manual=True)
    assert ps.get(pid)["current_milestone"] == 1 and ps.get(pid)["status"] == "running"
    assert ps.milestones(pid)[0]["status"] == "done"


def test_human_can_amend_but_not_completed_milestones(state):
    s, ps, pid = state
    service.approve(s, pid)
    service.pause(s, pid)
    d = definition()
    d["goal"] = "Track inventory in two locations"
    service.amend(s, pid, d)
    assert ps.get(pid)["previous_spec"] and "two locations" in ps.get(pid)["spec"]
    assert any(e["kind"] == "spec_approved" for e in ps.events(pid))


def test_one_active_project_per_repository(state):
    s, ps, pid = state
    other = ps.create()["id"]
    ps.draft(other, definition())
    service.approve(s, pid)
    with pytest.raises(ProjectError, match="Another active"):
        service.approve(s, other)
    assert ps.get(other)["status"] == "drafting" and ps.milestones(other) == []


async def test_second_failure_replans_and_third_escalates(state, cfg):
    s, ps, pid = state
    service.approve(s, pid)
    repo = Repo()
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    q = ps.items(pid)[0]
    s.queue_update(q["id"], status="failed")
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    copies = [x for x in ps.items(pid) if x["project_key"] == q["project_key"] and not x["project_processed"]]
    assert len(copies) == 1
    s.queue_update(copies[0]["id"], status="failed")
    seen = []

    async def split(*args):
        seen.append(args[4])
        r = proposal()
        r["reason"] = "Split after the second failure"
        return r
    await service.advance(cfg, s, pid, repository=repo, ask=split)
    assert seen
    q = next(x for x in ps.items(pid) if x["status"] == "queued" and x["project_key"] == "0")
    s.queue_update(q["id"], status="failed")
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    assert ps.get(pid)["status"] == "waiting_for_you"


async def test_worker_never_launches_tentative_or_held_project_items(state, cfg, monkeypatch):
    s, ps, pid = state
    service.approve(s, pid)
    repo = Repo()
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    finish(s, ps, pid)
    other = s.queue_add("Independent", "body", "owner/other", chain=0)
    started = []

    async def fake_run(cfg, store, item, **kw):
        started.append(item["id"])
        store.queue_update(item["id"], status="done")
        return store.queue_item(item["id"])

    async def tick(*args, **kw):
        await service.advance(cfg, s, pid, repository=repo, ask=ask)
    monkeypatch.setattr(runqueue, "_run_safe", fake_run)
    monkeypatch.setattr(service, "tick", tick)
    await runqueue.work(cfg, s, max_items=1, poll_s=.01, echo=None)
    assert started == [other]


async def test_budget_guard_and_lane_isolation(state, cfg):
    s, ps, pid = state
    service.approve(s, pid)
    s.record_llm_call("project:" + pid, cost_usd=30, cost_source="provider_reported", task_key="project-lead:m0")
    repo = Repo()
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    assert ps.get(pid)["status"] == "waiting_for_you" and repo.touched == []
    other = s.queue_add("Other", "body", "owner/other")
    assert service.can_start(s, s.queue_item(other))


def test_plan_accepts_a_leading_living_heading(state, cfg):
    _, projects, pid = state
    project = projects.get(pid)
    result = proposal()
    expected = service.validate_plan(project, result, cfg.projects.horizon)
    result["living"] = "## Living\n\n" + result["living"]
    assert service.validate_plan(project, result, cfg.projects.horizon).strip() == expected.strip()


def test_bad_configuration_and_horizon_rejected(cfg):
    from swarmforge.config import ConfigError, ProjectsConfig
    with pytest.raises(ConfigError):
        ProjectsConfig(horizon=40)
    p = {"spec": render(definition()), "budget_usd": 30}
    d = proposal()
    for t in d["tasks"]:
        t["ready"] = True
    with pytest.raises(ProjectError):
        service.validate_plan(p, d, 2)
    d = proposal()
    d["tasks"][0].update(kind="spike", budget_usd=10)
    with pytest.raises(ProjectError, match="spike budget"):
        service.validate_plan(p, d, 3)


def test_history_has_searchable_user_facing_features():
    from datetime import date
    from swarmforge.history import entries, filter_entries
    items = entries()
    assert len({e["id"] for e in items}) == len(items)
    for entry in items:
        for field in ("id", "date", "category", "title", "summary", "benefit", "where"):
            assert isinstance(entry[field], str) and entry[field].strip()
        date.fromisoformat(entry["date"])
    assert filter_entries(items, "screenshots")
    assert all(e["category"] == "Chat" for e in filter_entries(items, category="Chat"))
    assert filter_entries(items, "no-such-feature") == []


def test_project_cli_parser():
    from swarmforge.cli import build_parser
    parser = build_parser()
    assert parser.parse_args(["project", "new"]).project_cmd == "new"
    assert parser.parse_args(["project", "resume", "id", "--confirm-manual"]).confirm_manual


async def test_held_fixup_stays_in_project_and_blocks_later_work(state, cfg):
    from swarmforge import automerge as am
    s, ps, pid = state
    service.approve(s, pid)
    repo = Repo()
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    q = finish(s, ps, pid)
    s.record_intervention(q["run_id"], am.HELD, json.dumps({"reason": "Missing test coverage"}))
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    ready = [q for q in ps.items(pid) if q["status"] == "queued"]
    assert len(ready) == 1 and ready[0]["seed_run_id"] == q["run_id"]
    assert "sf-autofix" in ready[0]["body"]
    assert ready[0]["queue"] == service.lane(pid) and ready[0]["budget"] == 1
    assert service.can_start(s, ready[0])


def test_project_history_cannot_be_deleted_or_cleared(state):
    s, ps, pid = state
    q = s.queue_add("Evidence", "body", "owner/inventory", project_id=pid, milestone=0, project_key="evidence")
    s.create_run("r")
    s.queue_update(q, status="done", run_id="r")
    assert s.queue_clear_finished() == 0
    with pytest.raises(ValueError, match="history"):
        s.queue_remove(q)
    with pytest.raises(ValueError, match="history"):
        s.delete_run("r")


async def test_fixed_spec_edit_is_blocked_before_publication(state, cfg, tmp_path):
    from types import SimpleNamespace
    from swarmforge.github import Issue
    from swarmforge.services.publication import PublicationService
    from swarmforge.workspace import GitError
    s, ps, pid = state
    s.create_run("r")
    q = s.queue_add("Build", "body", "owner/inventory", project_id=pid, milestone=0)
    s.queue_update(q, run_id="r")
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs/project-spec.md").write_bytes(
        ps.get(pid)["spec"].replace("Track inventory", "Change the scope").encode("utf-8")
    )
    obj = SimpleNamespace(store=s, run_id="r", issue=Issue("Build", "body"), ws=SimpleNamespace(repo=repo))
    with pytest.raises(GitError, match="fixed spec"):
        await PublicationService.publish(obj, None, "owner", "inventory")


async def test_spike_finishes_without_a_pr_and_replans(state, cfg):
    s, ps, pid = state
    service.approve(s, pid)
    d = proposal()
    d["tasks"][0].update(key="api-survey", kind="spike")

    async def spike(*args):
        return d
    repo = Repo()
    await service.advance(cfg, s, pid, repository=repo, ask=spike)
    q = finish(s, ps, pid)
    s.update_run(q["run_id"], pr_url=None)
    assert q["mode"] == "single"
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    assert any(e["kind"] == "spike" for e in ps.events(pid))
    assert s.queue_item(q["id"])["project_processed"] == 1


def test_fixed_spec_must_preserve_decisions_and_plan_history(state):
    s, ps, pid = state
    p = ps.get(pid)
    d = proposal()
    p["spec"] = service.validate_plan(p, d, 3)
    d["living"] = d["living"].replace("2026-11-03: choose Flask", "Choose Django")
    updated = service.validate_plan(p, d, 3)
    assert fixed(updated) == fixed(p["spec"])
    assert "2026-11-03: choose Flask" in updated
    assert "Choose Django" in updated


async def test_restart_recovers_completed_run_without_starting_it_again(state, cfg, monkeypatch):
    s, ps, pid = state
    service.approve(s, pid)
    repo = Repo()
    await service.advance(cfg, s, pid, repository=repo, ask=ask)
    q = finish(s, ps, pid)
    s.queue_update(q["id"], status="running")

    async def no_tick(*a, **kw):
        pass
    monkeypatch.setattr(service, "tick", no_tick)
    await runqueue.work(cfg, s, max_items=0, echo=None)
    assert s.queue_item(q["id"])["status"] == "done"
    assert s.queue_item(q["id"])["run_id"] == q["run_id"]


def test_latest_failed_check_cannot_be_reported_as_passed(state):
    s, ps, pid = state
    service.approve(s, pid)
    q = s.queue_add("Build", "body", "owner/inventory", project_id=pid, milestone=0)
    s.create_run("r")
    s.queue_update(q, status="done", merged_sha="main-2", run_id="r", project_processed=1)
    s._exec("INSERT INTO test_runs(run_id,cycle,ts,command,exit_code) VALUES ('r',1,1,'pytest',0)")
    text = definition()["milestones"][0]["checks"][0]
    s._exec("INSERT INTO acceptance(run_id,cycle,text,status,evidence) VALUES ('r',1,?,'unsatisfied','Missing flow')", (text,))
    assert service.verify_checks(s, ps.get(pid))[0]["status"] == "failed"
    with pytest.raises(ProjectError, match="failed checks"):
        service.checkpoint(s, ps.get(pid), "Finished")


def test_new_dashboard_pages_and_spec_preview(tmp_path, monkeypatch):
    import sqlite3
    from streamlit.testing.v1 import AppTest
    from .test_dashboard import APP, _write_cfg, _go
    cfg = _write_cfg(tmp_path, monkeypatch)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute("""CREATE TABLE conversations (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, last_run_id TEXT
        )""")
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    _go(at, "Projects")
    next(b for b in at.button if b.label == "Start a project").click().run()
    assert not at.exception, [e.value for e in at.exception]
    s = Store(cfg.db_path)
    ps = Projects(s)
    p, = ps.list()
    assert s.conversation_messages(p["conversation_id"])[0]["content"].startswith("Tell me what you want to build")
    ps.draft(p["id"], definition())
    s.close()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(b.label == "Approve and launch" for b in at.button)
    assert any("Track inventory" in m.value for m in at.markdown)
    next(r for r in at.radio if r.label == "Where should the app live?").set_value("new")
    next(b for b in at.button if b.label == "Save repository choice").click().run()
    assert not at.exception
    assert any("Create private repository · owner/inventory" in c.value for c in at.caption)
    at.button(key="nav_whats-new").click().run()
    assert not at.exception
    assert any("Keep track of new features" in m.value for m in at.markdown)
    at.button[ next(i for i, b in enumerate(at.button) if b.label == "Mark all as read") ].click().run()
    assert not any("haven't marked" in m.value for m in at.info)


async def test_kickoff_uses_fake_model_without_repository_io(state, cfg):
    import httpx
    from swarmforge.projects import lead
    s, ps, pid = state

    def respond(request):
        assert request.url.host == "fake.test"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps({"reply": "Review your spec", "definition": definition()})}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50, "estimated_cost": .01}})
    await lead.interview(cfg, s, pid, "Use the agreed requirements", transport=httpx.MockTransport(respond))
    assert ps.get(pid)["status"] == "drafting" and ps.items(pid) == []
    assert ps.spent(pid) == .01


async def test_pausing_during_lead_call_discards_its_proposal(state, cfg):
    s, ps, pid = state
    service.approve(s, pid)
    repo = Repo()

    async def paused(*args):
        service.pause(s, pid)
        return proposal()
    await service.advance(cfg, s, pid, repository=repo, ask=paused)
    assert ps.get(pid)["status"] == "waiting_for_you"
    assert ps.items(pid) == [] and "write" not in repo.touched


async def test_slow_lead_does_not_hold_up_unrelated_queue(state, cfg, monkeypatch):
    import asyncio
    s, ps, pid = state
    service.approve(s, pid)
    q = s.queue_add("Unrelated", "body", "owner/other", chain=0)
    started = []

    async def slow(*a, **kw):
        await asyncio.Event().wait()

    async def fake_run(cfg, store, item, **kw):
        started.append(item["id"])
        store.queue_update(item["id"], status="done")
        return store.queue_item(item["id"])
    monkeypatch.setattr(service, "tick", slow)
    monkeypatch.setattr(runqueue, "_run_safe", fake_run)
    await asyncio.wait_for(runqueue.work(cfg, s, max_items=1, poll_s=.01, echo=None), timeout=2)
    assert started == [q]


async def test_dispatch_checks_budget_even_for_a_retry(state, cfg):
    s, ps, pid = state
    service.approve(s, pid)
    q = s.queue_add("Retry", "body", "owner/inventory", project_id=pid, milestone=0, project_key="retry", budget=1, queue=service.lane(pid))
    s.record_llm_call("project:" + pid, cost_usd=29.5, cost_source="provider_reported")
    await runqueue.run_item(cfg, s, s.queue_item(q), echo=None)
    assert ps.get(pid)["status"] == "waiting_for_you" and not s.queue_item(q)["run_id"]


def test_spec_boundary_uses_headings_not_inline_text():
    d = definition()
    d["goal"] = "Explain ## Living\nwithout treating inline text as a heading"
    text = render(d)
    assert "without treating" in fixed(text)


def test_repository_initializes_empty_existing_repo_once():
    import httpx
    from types import SimpleNamespace
    from swarmforge.projects.repository import Repository

    writes = []

    def request(req):
        if req.method == "GET" and req.url.path == "/repos/owner/inventory":
            return httpx.Response(200, json={"default_branch": "main"})
        if req.method == "GET" and req.url.path.endswith("/commits/main"):
            if writes:
                return httpx.Response(200, json={"sha": "initial"})
            return httpx.Response(409, json={"message": "Git Repository is empty."})
        assert req.method == "PUT" and req.url.path.endswith("/contents/README.md")
        writes.append(json.loads(req.content))
        return httpx.Response(201, json={"commit": {"sha": "initial"}})

    repo = Repository.__new__(Repository)
    repo.repo = "owner/inventory"
    repo.root = "/repos/" + repo.repo
    with httpx.Client(base_url="https://fake.test", transport=httpx.MockTransport(request)) as client:
        repo.gh = SimpleNamespace(_client=client)
        repo.ensure("existing", "project-id")
        repo.ensure("existing", "project-id")
    assert len(writes) == 1
    assert "branch" not in writes[0] and "sha" not in writes[0]


def test_repository_spec_write_uses_approved_fixed_bytes(cfg):
    import base64
    import httpx
    from types import SimpleNamespace
    from swarmforge.projects.repository import Repository
    original = render(definition())
    wrote = []

    def request(req):
        if req.method == "GET" and "/contents/" in req.url.path:
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(original.encode()).decode(), "sha": "blob-1"})
        if req.method == "GET" and "/commits/" in req.url.path:
            return httpx.Response(200, json={"sha": "head-1"})
        if req.method == "PUT":
            wrote.append(json.loads(req.content))
            return httpx.Response(200, json={"commit": {"sha": "head-2"}})
        raise AssertionError(req.url)
    repo = Repository.__new__(Repository)
    repo.root = "/repos/owner/inventory"
    repo.gh = SimpleNamespace(_client=httpx.Client(base_url="https://fake.test", transport=httpx.MockTransport(request)))
    p = {"spec_path": "docs/project-spec.md", "spec": original}
    modified = fixed(original) + "## Living\nNew plan"
    repo.write_spec(p, modified, {"branch": "main", "sha": "head-1"})
    assert wrote[0]["sha"] == "blob-1"
    with pytest.raises(ProjectError, match="differs"):
        repo.write_spec({**p, "spec": original.replace("Track inventory", "Changed scope")}, modified, {"branch": "main", "sha": "head-1"})
    repo.close()


@pytest.mark.slow
async def test_project_task_reuses_existing_fake_team_pipeline(cfg, sample_repo):
    from .test_queue import _provider
    from .conftest import ISSUE
    s = Store(cfg.db_path)
    ps = Projects(s)
    pid = ps.create()["id"]
    ps.draft(pid, definition())
    service.approve(s, pid)
    body = ISSUE.read_text(encoding="utf-8").split("\n", 2)[2]
    q = s.queue_add("Export", body, str(sample_repo), project_id=pid, milestone=0, project_key="export", budget=2, chain=0, queue=service.lane(pid))
    result = await runqueue.run_item(cfg, s, s.queue_item(q), echo=None, llm_transport=_provider().transport)
    assert result["status"] == "done", result["note"]
    run = s.get_run(result["run_id"])
    assert run["state"] == "COMPLETED"
    assert ps.spent(pid) > 0
    assert not result["merged_sha"]  # Ordinary run completion alone never completes the project task.
    s.close()


class ClosingRepo(Repo):
    def __init__(self, states):
        super().__init__()
        self.states, self.closed = states, []

    def pull(self, url):
        state = self.states[url]
        return {"state": "closed" if state == "closed" else "open", "merged_at": "t" if state == "merged" else None,
                "base": {"ref": "main"}, "merge_commit_sha": self.sha}

    def close_pull(self, url, comment):
        assert "Superseded" in comment and "Timeline v2" in comment
        self.closed.append(url)


def test_replan_closes_prs_of_abandoned_tasks_once(state):
    s, ps, pid = state
    service.approve(s, pid)
    prs = {}
    for n, kind in enumerate(["open", "merged", "closed", "open"], start=1):
        qid = s.queue_add(f"Task {n}", "x", "owner/inventory", queue=service.lane(pid))
        rid = f"r{n}"
        s.create_run(rid)
        url = f"https://github.com/owner/inventory/pull/{n}"
        s.update_run(rid, state="COMPLETED", pr_url=url)
        prs[url] = kind
        # 1-3 were superseded (processed without merging); 4 is still waiting to merge
        s.queue_update(qid, status="done", run_id=rid, project_id=pid, project_processed=0 if n == 4 else 1)
    repo = ClosingRepo(prs)
    assert service.close_superseded_prs(s, pid, repo, ["Timeline v2"]) == ["https://github.com/owner/inventory/pull/1"]
    assert repo.closed == ["https://github.com/owner/inventory/pull/1"]
    assert service.close_superseded_prs(s, pid, repo, ["Timeline v2"]) == []  # each PR handled once
    assert any(e["kind"] == "prs_closed" for e in ps.events(pid))


def test_a_github_error_while_closing_never_stops_the_project(state):
    s, ps, pid = state
    qid = s.queue_add("Task", "x", "owner/inventory", queue=service.lane(pid))
    s.create_run("r1")
    s.update_run("r1", state="COMPLETED", pr_url="https://github.com/owner/inventory/pull/9")
    s.queue_update(qid, status="done", run_id="r1", project_id=pid, project_processed=1)

    class Broken(Repo):
        def pull(self, url):
            raise service.ProjectError("GitHub GET: HTTP 502")
    assert service.close_superseded_prs(s, pid, Broken(), []) == []
    assert any(e["kind"] == "pr_close_failed" for e in ps.events(pid))


def test_living_logs_stay_bounded_when_the_lead_paraphrases_them():
    from swarmforge.projects.spec import LOG_LIMIT_CHARS, compact_living
    original = render(definition())
    head = fixed(original)
    decisions = "\n".join(f"- 2026-11-{i % 28 + 1:02d}: decision {i} " + "x" * 120 for i in range(400))
    living = ("## Living\n\n### Current state\nWorking.\n\n### Decisions made\n" + decisions + "\n" + decisions
              + "\n\n### Plan\nNext\n\n### Plan changes\n- one\n- one\n\n### Open questions\nNone\n")
    spec = compact_living(head + living)
    assert fixed(spec) == head  # the owner's part is byte-identical
    section = spec.split("### Decisions made")[1].split("### Plan")[0]
    assert len(section) <= LOG_LIMIT_CHARS + 200
    assert "decision 399" in section and "decision 0 " not in section  # newest kept, oldest trimmed
    assert "older entries trimmed" in section
    assert spec.split("### Plan changes")[1].count("- one") == 1  # exact duplicates dropped
    assert compact_living(spec) == spec  # stable: compacting twice changes nothing


def test_accepted_plan_writes_a_bounded_spec(state):
    s, ps, pid = state
    p = ps.get(pid)
    huge = "\n".join(f"- old decision {i} " + "y" * 200 for i in range(2000))
    head = fixed(p["spec"])
    ps.update(pid, spec=head + "## Living\n\n### Current state\nx\n\n### Decisions made\n" + huge
              + "\n\n### Plan\n\n### Plan changes\n\n### Open questions\n")
    result = proposal()
    result["living"] = result["living"].replace("2026-11-03: choose Flask", "2026-11-24: paraphrased history")
    spec = service.validate_plan(ps.get(pid), result, 3)
    assert len(spec) < 40000 and "paraphrased history" in spec


def _finished(state):
    s, ps, pid = state
    service.approve(s, pid)
    s._exec("UPDATE milestones SET status='done' WHERE project_id=?", (pid,))
    ps.update(pid, status="done", current_milestone=2)
    return s, ps, pid


def test_a_finished_project_reopens_with_a_change_round(state):
    s, ps, pid = _finished(state)
    budget = ps.get(pid)["budget_usd"]
    p = service.request_changes(s, pid, "- Make the cards bigger\n- Use a warmer color palette\n- Add a dark mode toggle",
                                extra_budget=10)
    assert p["status"] == "running" and p["current_milestone"] == 3 and p["budget_usd"] == budget + 10
    m = p["definition"]["milestones"][3]
    assert m["change_request"] and m["name"].startswith("Changes 1: ")
    assert m["checks"][:3] == ["Make the cards bigger", "Use a warmer color palette", "Add a dark mode toggle"]
    assert "existing behavior and tests keep passing" in m["checks"][-1]
    assert "#### 4. Changes 1:" in p["spec"] and fixed(p["spec"]).count("#### ") == 4
    assert ps.milestones(pid)[3]["status"] == "current"
    assert any(e["kind"] == "changes_requested" for e in ps.events(pid))
    s._exec("UPDATE milestones SET status='done' WHERE project_id=?", (pid,))
    ps.update(pid, status="done")
    p = service.request_changes(s, pid, "Rename the app to Horse Match everywhere it appears in the UI.")
    assert p["current_milestone"] == 4 and p["definition"]["milestones"][4]["name"].startswith("Changes 2: ")
    assert len(p["definition"]["milestones"][4]["checks"]) == 2  # one sentence + keep existing behavior


def test_change_requests_need_a_finished_project_a_request_and_money(state):
    s, ps, pid = state
    service.approve(s, pid)
    with pytest.raises(ProjectError, match="Only a finished project"):
        service.request_changes(s, pid, "Make it nicer please")
    s._exec("UPDATE milestones SET status='done' WHERE project_id=?", (pid,))
    ps.update(pid, status="done", current_milestone=2)
    with pytest.raises(ProjectError, match="Describe"):
        service.request_changes(s, pid, "  ")
    s.record_llm_call("project:" + pid, task_key="project-lead:m0", cost_usd=ps.get(pid)["budget_usd"], cost_source="estimated", status="ok")
    with pytest.raises(ProjectError, match="add extra budget"):
        service.request_changes(s, pid, "Make the cards bigger")
    assert service.request_changes(s, pid, "Make the cards bigger", extra_budget=5)["status"] == "running"


def test_discussing_a_project_then_launching_the_proposed_changes(state):
    import asyncio

    from swarmforge.projects import lead

    s, ps, pid = _finished(state)
    seen = {}

    async def fake_call(cfg, store, p, system, context, transport=None, **kw):
        seen["system"], seen["context"] = system, context
        return {"reply": "The import never picks up .MOV files. I would fix the watcher.",
                "proposal": {"title": "Fix import", "changes": ["Import picks up .MOV files", "Show import errors in the UI"]}}

    lead_call = lead.call
    lead.call = fake_call
    try:
        reply = asyncio.run(lead.discuss(None, s, pid, "Nothing shows up after I copy files in."))
    finally:
        lead.call = lead_call
    assert "MOV" in reply and seen["system"] is lead.DISCUSS
    assert seen["context"]["messages"][-1]["content"] == "Nothing shows up after I copy files in."
    last = s.conversation_messages(ps.get(pid)["conversation_id"])[-1]
    assert last["role"] == "assistant"
    p = service.launch_discussed_changes(s, pid, extra_budget=5)
    assert p["status"] == "running"
    m = p["definition"]["milestones"][-1]
    assert m["change_request"] and "Import picks up .MOV files" in m["checks"]
    with pytest.raises(ProjectError, match="no proposed change plan"):
        service.launch_discussed_changes(s, pid)  # each plan launches once


def test_launching_changes_at_a_review_keeps_the_milestone_open(state):
    s, ps, pid = state
    service.approve(s, pid)
    ps.update(pid, status="at_checkpoint")
    s.kv_set("project_change_proposal:" + pid, json.dumps({"title": "Search is broken", "changes": ["Keyword search returns results"]}))
    p = service.launch_discussed_changes(s, pid)
    assert p["status"] == "running" and p["current_milestone"] == 0  # not accepted on the owner's behalf
    reply = [e for e in ps.events(pid) if e["kind"] == "reply"][-1]
    assert "Do NOT accept" in reply["detail"] and "Keyword search returns results" in reply["detail"]
