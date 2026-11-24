"""Interactive disposable workspace: fake models and worker, never live configuration."""
import asyncio
import time
from pathlib import Path

from ..db import Store
from ..projects.store import Projects
from ..services.dashboard import DashboardActions


DEMO_REPLY_DELAY_S = 1.5


def preview_image():
    import struct
    import zlib
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    width, height = 480, 160
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            color = (24, 28, 34)
            for left, top, rgb in ((35, 95, (255, 128, 60)), (160, 55, (83, 191, 157)), (285, 25, (103, 154, 230))):
                if left < x < left + 90 and top < y < 140:
                    color = rgb
            row.extend(color)
        rows.append(bytes(row))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')


async def fake_answer(config, store, question, *, conversation, attachments=None, on_tool=None, question_saved=False, on_text=None, request_id=None):
    import asyncio
    from ..chatbot import ChatError
    if not question_saved:
        store.conversation_append(conversation, "user", question)
    if on_tool:
        on_tool("status_snapshot", {}, "Demo status read")
    await asyncio.sleep(DEMO_REPLY_DELAY_S)
    if "fail" in question.lower():
        raise ChatError("Demo failure: the simulated model did not answer. Nothing was charged. Send the message again.")
    if on_tool:
        on_tool("work_items", {}, "Demo work list")
    names = ", ".join(Path(a).name for a in attachments or [])
    text = "Demo reply — no model was called. " + (f"I received {names}. " if names else "") + "Here is a Python example and two files to try:\n\n```python\nfrom pathlib import Path\n\nrows = 'item,count\\ncoffee,3\\nbeans,7\\n'\nPath('inventory.csv').write_text(rows, encoding='utf-8')\n```\n\n| Item | Count |\n| --- | --- |\n| Coffee | 3 |\n| Beans | 7 |\n\nThe CSV and image preview are attached below. You can copy the code, save this reply as Markdown, or download either file."
    if on_text:
        for end in range(18, len(text) + 18, 18):
            on_text(text[:end])
            await asyncio.sleep(.05)
    import base64
    from ..chat_files import ReplyFiles
    files = ReplyFiles(config, store, conversation, request_id)
    files.create("inventory.csv", "item,count\ncoffee,3\nbeans,7\n")
    files.create("preview.png", base64.b64encode(preview_image()).decode(), encoding="base64")
    with store.transaction():
        mid = store.conversation_append(conversation, "assistant", text)
        files.attach(mid)
    from ..chatbot import auto_title, needs_title
    if needs_title(store, conversation):
        await auto_title(store, conversation)  # no model in the demo: title from the first message
    return {"answer": "Demo reply"}


async def fake_interview(config, store, pid, question, *, expected_spec=None, question_saved=False, on_text=None):
    import copy
    projects = Projects(store)
    p = projects.get(pid)
    if not question_saved:
        store.conversation_append(p["conversation_id"], "user", question)
    turns = sum(m["role"] == "user" for m in store.conversation_messages(p["conversation_id"]))
    from ..projects import kickoff
    fields = []
    if not p["definition"] and turns < 2:
        reply = "Who will use it, and what should they be able to do? Tell me the repository, budget and constraints too, if you know them. This is a simulated demo conversation; nothing launches."
        fields = [{"id": "priority", "label": "Which should the preview prioritize?", "kind": "select", "required": False, "options": ["A working core workflow", "The visual design"]}]
    else:
        template = p["definition"] or next((other["definition"] for other in projects.list() if other["definition"]), None)
        if template:
            projects.draft(pid, copy.deepcopy(template), expected_spec=expected_spec)
        reply = "The demo proposal is ready for review. Its sample scope, repository and budget are fabricated; no lead model was called. Keep chatting to refine it, or open Review proposal before approving a simulated launch."
    if on_text:
        for end in range(18, len(reply) + 18, 18):
            on_text(reply[:end])
            await asyncio.sleep(.04)
    store.conversation_append(p["conversation_id"], "assistant", reply, actions={"project_questions": fields, "kickoff_defaults": kickoff.defaults(config)})


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


def seed_workflow(store, rid, now, state):
    """A recorded sample graph for UI checks; no worker or model is started."""
    terminal = state in ("COMPLETED", "FAILED")
    titles = [("T1", "Map task events and progress data", "developer", [], "INTEGRATED"),
              ("T2", "Build the progress overview", "developer", ["T1"], "INTEGRATED"),
              ("T3", "Design accessible bot and sequence cards", "designer", ["T1"], "INTEGRATED"),
              ("T4", "Polish task details on phone screens", "developer", ["T2", "T3"],
               "INTEGRATED" if state == "COMPLETED" else "FAILED" if terminal else "RUNNING"),
              ("T5", "Document the new task workspace", "developer", ["T4"],
               "INTEGRATED" if state == "COMPLETED" else "BLOCKED" if terminal else "PENDING")]
    for i, (key, title, role, deps, task_state) in enumerate(titles):
        done = task_state == "INTEGRATED"
        store.upsert_task(rid, key, title=title, role=role, model=f"simulated-{role}", depends_on=deps,
                          expected_files=["frontend/src/Work.tsx"], state=task_state, attempts=1 if done or key == "T4" else 0,
                          started_at=now - 500 + i * 55 if task_state != "PENDING" else None,
                          ended_at=now - 300 + i * 55 if done else None,
                          summary="Sample result: implementation and focused checks retained." if done else None,
                          error="Sample failure: the phone layout overflowed at 390 px." if task_state == "FAILED" else None)
    for index, phase in enumerate(("REPOSITORY_INSPECTION", "PLANNING", "TASK_VALIDATION", "TASK_EXECUTION")):
        store._exec("INSERT INTO events(run_id,ts,level,kind,message,data) VALUES (?,?,?,?,?,?)",
                    (rid, now - 600 + index * 60, "info", "state", f"Demo stage -> {phase}", '{"to":"' + phase + '"}'))
    if terminal:
        store.event(rid, "state", f"Demo stage -> {state}", data={"to": state})
    for role, calls, cost in (("planner", 3, .08), ("developer", 8, .36), ("designer", 4, .12), ("tester", 1, .04)):
        for i in range(calls):
            store.record_llm_call(rid, role=role, provider="demo", model=f"simulated-{role}", status="ok",
                                  total_tokens=650, cost_source="estimated", cost_usd=cost / calls,
                                  started_at=now - 600 + i * 15, ended_at=now - 590 + i * 15)
    with store.transaction() as db:
        db.execute("INSERT INTO execution_allowances(id,created_at,deadline,budget_usd,spent_usd,max_recoveries,no_progress_s,repeat_limit) "
                   "VALUES (?,?,?,?,?,?,?,?)", (rid, now - 600, now + 3600, 5, .6, 2, 600, 3))
        db.execute("INSERT INTO execution_attempts(run_id,execution_id,number,started_at,last_progress_at,current_action,last_result,next_action) "
                   "VALUES (?,?,1,?,?,?,?,?)", (rid, rid, now - 600, now - 30,
                    "Review the completed run" if terminal else "Polishing the phone layout in T4",
                    "T2 and T3 were integrated; 12 focused checks passed.",
                    "Review retained evidence" if terminal else "T5 documentation follows T4, then final verification."))


def create_demo(root: Path):
    import yaml
    from ..config import parse_config
    raw = {"providers": {"demo": {"base_url": "http://127.0.0.1:0", "api_key_env": "SWARMFORGE_DEMO_UNUSED"}},
           "roles": {role: {"provider": "demo", "model": f"simulated-{role} (no API calls)"}
                     for role in ("planner", "developer", "tester", "reviewer", "librarian")},
           "developer_roles": ["developer"], "projects": {"default_repo_owner": "example"}, "workdir": str(root)}
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
    fixtures = [("demo-running", "Make the task timeline easier to follow", "TASK_EXECUTION", "running"),
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
        seed_workflow(store, rid, now, state)
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
    with store.transaction():
        store.conn.execute("INSERT INTO acceptance(run_id,cycle,criterion_id,text,status,evidence) VALUES ('demo-running',0,'demo-check','A user can open retained evidence','passed','Fabricated demo evidence, not a real verification run')")
    definition = {"name": "Inventory app · demo", "goal": "Build a small inventory workspace", "users": "A small shop team", "repo": "example/inventory",
                  "repository_kind": "existing", "out_of_scope": "Payments", "stack": "React and Python", "hosting": "Local", "deadline": "None",
                  "latitude": "Choose presentation details", "ask_before": "Changing scope or budget", "budget_usd": 30,
                  "milestones": [{"name": name, "goal": goal, "checks": ["A user can complete the core journey", "Recorded checks cover the behavior"], "estimate_usd": 10, "estimate_hours": 2}
                                 for name, goal in [("Browse inventory", "List and search items"), ("Edit inventory", "Make validated changes"), ("Export reports", "Export selected records")]]}
    project = Projects(store).create(definition["name"])
    Projects(store).draft(project["id"], definition)
    from ..projects import service as project_service
    recovery_project = Projects(store).create("Planning recovery demo")
    recovery_definition = {**definition, "name": "Planning recovery demo", "repo": "example/recovery-demo"}
    Projects(store).draft(recovery_project["id"], recovery_definition)
    project_service.approve(store, recovery_project["id"])
    for ordinal in (1, 2):
        store._exec("INSERT INTO project_decisions(project_id,epoch,ordinal,created_at,finished_at,action,status,reason,evidence,proposal,result) "
                    "VALUES (?,?,?,?,?,'investigate','rejected',?,'{}',?,?)",
                    (recovery_project["id"], "demo-recovery", ordinal, now, now, "Fabricated sample: research an existing task",
                     json.dumps({"action": "investigate", "task": "probe", "hypothesis": "Compare the options"}),
                     json.dumps({"error": "Lead action contains fields outside its typed contract: hypothesis"})))
    project_service.stop(Projects(store), Projects(store).get(recovery_project["id"]),
                         "Lead returned two invalid proposals. Demo data only; no model was called.", {})
    store.conversation_create("demo-chat", "Investigate the current task")
    store.conversation_append("demo-chat", "assistant", "This is a disposable demo conversation. Sending a message generates a simulated reply; no model is called.")
    actions = DashboardActions(store, config, chat_answer=fake_answer, project_interview=fake_interview, worker_start=fake_worker,
                               diagnose=fake_diagnose, pr_api=FakePullRequests(), model_test=fake_model_test)
    actions.health_checks = fake_health
    actions.provider_models = lambda config, provider: [f"simulated-{r} (no API calls)" for r in config.roles] + ["simulated-alternative (no API calls)"]
    return config, store, actions
