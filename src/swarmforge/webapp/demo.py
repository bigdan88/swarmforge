"""Interactive disposable workspace: fake models and worker, never live configuration."""
import asyncio
import time
from pathlib import Path

from ..db import Store
from ..projects.store import Projects
from ..services.dashboard import DashboardActions


DEMO_REPLY_DELAY_S = 1.5


async def fake_answer(config, store, question, *, conversation, attachments=None, on_tool=None):
    import asyncio
    from ..chatbot import ChatError
    store.conversation_append(conversation, "user", question)
    if on_tool:
        on_tool("status_snapshot", {}, "Demo status read")
    await asyncio.sleep(DEMO_REPLY_DELAY_S)
    if "fail" in question.lower():
        raise ChatError("Demo failure: the simulated model did not answer. Nothing was charged. Send the message again.")
    if on_tool:
        on_tool("work_items", {}, "Demo work list")
    names = ", ".join(Path(a).name for a in attachments or [])
    store.conversation_append(conversation, "assistant", "Demo reply — no model was called. " + (f"I received {names}. " if names else "")
                              + "Your running task is in testing; a queued task is waiting. Open Work to inspect the recorded checks and report, or create a project draft to try its approval flow.")
    return {"answer": "Demo reply"}


async def fake_interview(config, store, pid, question, *, expected_spec=None):
    p = Projects(store).get(pid)
    store.conversation_append(p["conversation_id"], "user", question)
    store.conversation_append(p["conversation_id"], "assistant", "Demo project reply — no lead model was called and nothing launched. Use Edit draft to change the milestones, then review the full spec before approving it.")


async def fake_diagnose(config, store, run_id):
    store.failure_save(run_id, title="Handle empty repository responses", repo="example/inventory", state="FAILED",
                         reason="Demo sample data", category="tests_failing",
                         root_cause="Demo diagnosis (no model was called): the empty-repository branch returned None, so two acceptance tests failed.",
                         fix="Return an empty list for repositories without commits and cover it with a regression test.",
                         lesson="Treat 'no data' as a valid response shape, not an error.", retry=0, diagnosed_by="demo",
                         evidence="FAILED tests/test_repo.py::test_empty_repository - TypeError: 'NoneType' object is not iterable",
                         outcome="needs you: retry after reviewing the fix", cost_usd=0.0)
    return True


class FakePullRequests:
    """Simulated GitHub: nothing leaves this machine."""
    has_token = True

    def __init__(self):
        from ..prs import PR
        self.open = {pr.number: pr for pr in [
            PR("example/inventory", 42, "Export filtered reports as CSV", "https://github.com/example/inventory/pull/42",
               "swarmforge/task-demo1", "main", False, "swarmforge-bot", "2026-11-20T10:00:00Z", "n42", "a1b2c3d4e5", "clean", True, 120, 14, 5, "demo-done"),
            PR("example/inventory", 43, "Add keyboard shortcuts", "https://github.com/example/inventory/pull/43",
               "swarmforge/task-demo2", "main", False, "swarmforge-bot", "2026-11-21T09:00:00Z", "n43", "b2c3d4e5f6", "behind", True, 40, 2, 2),
            PR("example/inventory", 44, "Rename inventory fields", "https://github.com/example/inventory/pull/44",
               "swarmforge/task-demo3", "main", False, "swarmforge-bot", "2026-11-21T12:00:00Z", "n44", "c3d4e5f6a7", "dirty", False, 9, 9, 1)]}

    def list(self, repo, store):
        import copy
        return [copy.copy(pr) for pr in self.open.values() if pr.repo == repo]

    def merge(self, pr, method, delete_branch):
        self.open.pop(pr.number, None)

    def update(self, pr):
        current = self.open[pr.number]
        current.state, current.head_sha = "clean", current.head_sha[::-1]


def fake_health(config):
    return [{"name": "Provider API keys", "status": "ok", "message": "Demo: no keys are needed"},
            {"name": "Docker running", "status": "skipped", "message": "Demo: Docker is not checked", "optional": True},
            {"name": "GitHub access", "status": "fail", "message": "Demo: no token configured", "fix": "gh auth login"}]


def fake_model_test(config, roles=None):
    return [{"role": r, "model": config.roles[r].model, "ok": True, "detail": "report_status({\"status\": \"ready\"}) — simulated", "cost_usd": 0.0}
            for r in (roles or list(config.roles))]


def fake_worker(config, store):
    store.kv_set("queue_paused", "0")
    return None


def create_demo(root: Path):
    import yaml
    from ..config import parse_config
    raw = {"providers": {"demo": {"base_url": "http://127.0.0.1:0", "api_key_env": "SWARMFORGE_DEMO_UNUSED"}},
           "roles": {role: {"provider": "demo", "model": f"simulated-{role} (no API calls)"}
                     for role in ("planner", "developer", "tester", "reviewer", "librarian")},
           "developer_roles": ["developer"], "workdir": str(root)}
    source = root / "demo.yaml"
    source.write_text("# Disposable demo; no credentials or live configuration.\n" + yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = parse_config(raw, source_path=source)
    config.knowledge.resolved_vault = root / "knowledge"
    config.knowledge.resolved_vault.mkdir()
    (config.knowledge.resolved_vault / "example-inventory").mkdir()
    (config.knowledge.resolved_vault / "example-inventory" / "Empty repositories return lists.md").write_text(
        "---\ntitle: Empty repositories return an empty list\ntype: pitfall\nrepo: example/inventory\ntags: [api, demo]\nsource: user\n---\n\n"
        "Demo note in a nested folder. Callers iterate the result, so return [] rather than None.\n", encoding="utf-8")
    (config.knowledge.resolved_vault / "Demo lessons.md").write_text("# Keep evidence with the task\n\nThis is a demo knowledge note. Retain test results and acceptance evidence, and distinguish recorded checks from claims that have not been verified.\n", encoding="utf-8")
    store = Store(config.db_path)
    now = time.time()
    fixtures = [("demo-running", "Make the task timeline easier to follow", "TESTING", "running"),
                ("demo-done", "Export filtered reports as CSV", "COMPLETED", "done"),
                ("demo-failed", "Handle empty repository responses", "FAILED", "failed")]
    for rid, title, state, status in fixtures:
        directory = root / "runs" / rid
        directory.mkdir(parents=True)
        (directory / "report.md").write_text(f"# {title}\n\nDemo report — fabricated sample data, not verification of a real task.\n\n## Outcome\n{state}\n\n## Evidence\nThe web interface can browse retained reports without changing them.\n", encoding="utf-8")
        store.create_run(rid, issue_title=title, repo="example/inventory", run_dir=str(directory), budget_usd=5)
        store.update_run(rid, state=state, state_reason="Demo sample data", finished_at=now if status != "running" else None)
        store.queue_add(title, "Demo task; no real repository operations.", "example/inventory", run_id=rid, status=status, budget=5)
        store.event(rid, "verification", "Demo evidence only. No real build or model call was made.")
    asyncio.run(fake_diagnose(config, store, "demo-failed"))
    import json
    sample = [{"case": f"case-{c}", "repeat": 0, "team": team, "mode": "swarm", "final_state": "COMPLETED" if ok else "FAILED",
               "hidden_acceptance": {"passed": 5 if ok else 2, "total": 5}, "elapsed_s": secs, "cost_usd": cost, "repair_cycles": 1,
               "human_interventions": 0, "calls_without_cost": 0}
              for team, results in (("chinese", [(True, 1500, .6), (True, 1800, .7), (False, 2400, .9)]),
                                    ("american-open", [(True, 1300, .9), (False, 2100, 1.1), (False, 2600, 1.2)]))
              for c, (ok, secs, cost) in enumerate(results)]
    store._exec("INSERT INTO benchmarks(created_at,label,issue,results_json) VALUES (?,?,?,?)",
                (now, "Demo suite (fabricated)", "bench/suite.yaml", json.dumps(sample)))
    store.update_run("demo-done", pr_url="https://github.com/example/inventory/pull/42")
    store.queue_add("Add keyboard shortcuts to project navigation", "Demo task", "example/inventory", budget=5)
    store.record_test_run("demo-running", label="Demo unit checks", exit_code=0, passed=12, failed=0, errors=0, skipped=0)
    with store.conn:
        store.conn.execute("INSERT INTO acceptance(run_id,cycle,criterion_id,text,status,evidence) VALUES ('demo-running',0,'demo-check','A user can open retained evidence','passed','Fabricated demo evidence, not a real verification run')")
    definition = {"name": "Inventory app · demo", "goal": "Build a small inventory workspace", "users": "A small shop team", "repo": "example/inventory",
                  "repository_kind": "existing", "out_of_scope": "Payments", "stack": "React and Python", "hosting": "Local", "deadline": "None",
                  "latitude": "Choose presentation details", "ask_before": "Changing scope or budget", "budget_usd": 30,
                  "milestones": [{"name": name, "goal": goal, "checks": ["A user can complete the core journey", "Recorded checks cover the behavior"], "estimate_usd": 10, "estimate_hours": 2}
                                 for name, goal in [("Browse inventory", "List and search items"), ("Edit inventory", "Make validated changes"), ("Export reports", "Export selected records")]]}
    project = Projects(store).create(definition["name"])
    Projects(store).draft(project["id"], definition)
    store.conversation_create("demo-chat", "Investigate the current task")
    store.conversation_append("demo-chat", "assistant", "This is a disposable demo conversation. Sending a message generates a simulated reply; no model is called.")
    actions = DashboardActions(store, config, chat_answer=fake_answer, project_interview=fake_interview, worker_start=fake_worker,
                               diagnose=fake_diagnose, pr_api=FakePullRequests(), model_test=fake_model_test)
    actions.health_checks = fake_health
    actions.provider_models = lambda config, provider: [f"simulated-{r} (no API calls)" for r in config.roles] + ["simulated-alternative (no API calls)"]
    return config, store, actions
