"""Interactive disposable workspace: fake models and worker, never live configuration."""
import time
from pathlib import Path

from ..config import Config, ProviderConfig, RoleConfig
from ..db import Store
from ..projects.store import Projects
from ..services.dashboard import DashboardActions


async def fake_answer(config, store, question, *, conversation):
    store.conversation_append(conversation, "user", question)
    store.conversation_append(conversation, "assistant", "Demo reply — no model was called. Your running task is in testing; a queued task is waiting. Open Work to inspect the recorded checks and report, or create a project draft to try its approval flow.")
    return {"answer": "Demo reply"}


async def fake_interview(config, store, pid, question, *, expected_spec=None):
    p = Projects(store).get(pid)
    store.conversation_append(p["conversation_id"], "user", question)
    store.conversation_append(p["conversation_id"], "assistant", "Demo project reply — no lead model was called and nothing launched. Use Edit draft to change the milestones, then review the full spec before approving it.")


def fake_worker(config, store):
    store.kv_set("queue_paused", "0")
    return None


def create_demo(root: Path):
    config = Config(providers={"demo": ProviderConfig("demo", "http://127.0.0.1:0", "SWARMFORGE_DEMO_UNUSED")},
                    roles={"planner": RoleConfig("planner", "demo", "Simulated model — no API calls")}, developer_roles=[], workdir=root)
    config.source_path = root / "demo.yaml"
    config.source_path.write_text("# Disposable demo; no credentials or live configuration.\n", encoding="utf-8")
    config.knowledge.resolved_vault = root / "knowledge"
    config.knowledge.resolved_vault.mkdir()
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
    return config, store, DashboardActions(store, config, chat_answer=fake_answer, project_interview=fake_interview, worker_start=fake_worker)
