"""Run progress projections use disposable recorded data, never models or repositories."""
import json

from swarmforge.db import Store
from swarmforge.webapp.contracts import WorkEvidence
from swarmforge.webapp.queries import WorkReader


def history(tmp_path):
    path = tmp_path / "work.db"
    store = Store(path)
    store.create_run("run1", issue_title="A recorded run", config_json='{"api_key":"secret-config"}')
    store.update_run("run1", state="TASK_EXECUTION", repair_cycle=1)
    store.upsert_task("run1", "T1", title="Build", role="developer", model="model-a", state="INTEGRATED",
                      depends_on=[], expected_files=["app.py"], summary="Built", worktree="C:/private/worktree")
    store.upsert_task("run1", "R1", title="Repair", cycle=1, role="developer", model="model-b", state="RUNNING",
                      depends_on=["T1"], expected_files=[], attempts=2)
    store.create_run("other", issue_title="Another run")
    store.upsert_task("other", "X", title="Other task", state="RUNNING")
    store.record_llm_call("run1", role="developer", model="model-a", provider="fake", total_tokens=30,
                          cost_usd=.2, cost_source="provider_reported", status="ok", started_at=10, ended_at=20)
    store.record_llm_call("run1", role="developer", model="model-b", provider="fake", total_tokens=40,
                          cost_usd=None, cost_source="unavailable", status="error", error="private call error")
    store.record_llm_call("other", role="private-role", model="other-model")
    store.event("run1", "state", "Inspect -> PLANNING", data={"to": "PLANNING", "secret": "secret-event-data"})
    store.event("run1", "state", "Plan -> TASK_EXECUTION", data={"to": "TASK_EXECUTION"})
    store.close()
    return path


def test_recorded_graph_models_and_phase_history_are_public_and_read_only(tmp_path):
    path = history(tmp_path)
    before = path.read_bytes()
    result = WorkReader(path).evidence("run-run1")
    WorkEvidence.model_validate(result)
    execution = result["execution"]
    assert execution["counts"] == {"INTEGRATED": 1, "RUNNING": 1}
    assert execution["repair_cycle"] == 1
    assert [(t["task_key"], t["depends_on"]) for t in execution["tasks"]] == [("T1", []), ("R1", ["T1"])]
    assert execution["tasks"][1]["model"] == "model-b"
    assert all(t["dependencies_complete"] for t in execution["tasks"])
    bots = {b["model"]: b for b in execution["bots"]}
    assert bots["model-a"]["known_cost_usd"] == .2
    assert bots["model-b"]["unavailable_calls"] == bots["model-b"]["errors"] == 1
    assert [p["state"] for p in execution["phases"]] == ["PLANNING", "TASK_EXECUTION"]
    assert WorkReader(path).evidence("run-run1", after=result["next_event_id"])["execution"] == execution
    for private in ("secret-config", "secret-event-data", "private call error", "worktree", "private-role", "other-model", "config_json"):
        assert private not in json.dumps(result)
    assert path.read_bytes() == before


def test_task_preview_bounds_keep_whole_run_counts_and_flag_missing_context(tmp_path):
    path = history(tmp_path)
    store = Store(path)
    for i in range(205):
        store.upsert_task("run1", f"A{i}", state="PENDING", depends_on=["T1"], expected_files=[])
    store.upsert_task("run1", "A204", summary="x" * 5000, depends_on=list(map(str, range(60))),
                      expected_files=["f" * 400] * 30)
    store.close()
    execution = WorkReader(path).evidence("run-run1")["execution"]
    assert len(execution["tasks"]) == 200 and execution["tasks_has_more"]
    assert sum(execution["counts"].values()) == 207
    task = next(t for t in execution["tasks"] if t["task_key"] == "A204")
    assert len(task["summary"]) == 1200 and task["truncated"]
    assert len(task["depends_on"]) == 50 and not task["dependencies_complete"]
    assert len(task["expected_files"]) == 20 and len(task["expected_files"][0]) == 240


def test_invalid_legacy_dependencies_and_phase_data_do_not_break_progress(tmp_path):
    path = history(tmp_path)
    store = Store(path)
    store.upsert_task("run1", "T1", depends_on="{broken", expected_files='{"not":"a list"}')
    store.event("run1", "state", "Not a state", data={"to": "secret-unknown-state"})
    store.event("run1", "state", "Legacy -> TESTING", data="malformed legacy data")
    store.close()
    result = WorkReader(path).evidence("run-run1")
    WorkEvidence.model_validate(result)
    task = result["execution"]["tasks"][0]
    assert task["depends_on"] == task["expected_files"] == []
    assert task["truncated"] and not task["dependencies_complete"]
    assert result["execution"]["phases"][-1]["state"] == "TESTING"
    assert "secret-unknown-state" not in json.dumps(result["execution"])


def test_undispatched_and_empty_runs_remain_honest(tmp_path):
    path = history(tmp_path)
    store = Store(path)
    queue_id = store.queue_add("Waiting", "Private task body", "owner/repo")
    store.create_run("empty")
    store.close()
    reader = WorkReader(path)
    pending = reader.evidence(f"task-{queue_id}")
    assert pending["execution"] is None
    empty = reader.evidence("run-empty")["execution"]
    assert empty["bots"] == empty["phases"] == []
    assert empty["counts"] == {}


def test_bot_and_phase_previews_are_bounded_without_changing_usage_totals(tmp_path):
    path = history(tmp_path)
    store = Store(path)
    for i in range(45):
        store.record_llm_call("run1", role="developer", model=f"model-{i}", cost_source="estimated", cost_usd=.1)
    for _ in range(110):
        store.event("run1", "state", "Build -> TESTING", data={"to": "TESTING"})
    store.close()
    snapshot = WorkReader(path).evidence("run-run1")
    execution = snapshot["execution"]
    assert len(execution["bots"]) == 40 and execution["bots_has_more"]
    assert len(execution["phases"]) == 100 and execution["phases_has_more"]
    assert snapshot["costs"]["calls"] == 47


def test_legacy_queue_reference_without_a_run_record_keeps_available_evidence(tmp_path):
    store = Store(tmp_path / "legacy.db")
    queue_id = store.queue_add("Retained queue history", "", "owner/repo", run_id="missing", status="failed")
    store.close()
    snapshot = WorkReader(tmp_path / "legacy.db").evidence(f"task-{queue_id}")
    WorkEvidence.model_validate(snapshot)
    assert snapshot["run_id"] == "missing" and snapshot["execution"] is None
    assert snapshot["costs"]["calls"] == 0


def test_disposable_demo_has_inspectable_sequence_without_model_calls(tmp_path):
    from swarmforge.webapp.demo import seed_workflow
    store = Store(tmp_path / "demo.db")
    store.create_run("demo")
    store.update_run("demo", state="TASK_EXECUTION")
    seed_workflow(store, "demo", 1000, "TASK_EXECUTION")
    store.close()
    snapshot = WorkReader(tmp_path / "demo.db").evidence("run-demo")
    WorkEvidence.model_validate(snapshot)
    assert snapshot["execution"]["counts"] == {"INTEGRATED": 3, "RUNNING": 1, "PENDING": 1}
    assert snapshot["allowance"]["next_action"].startswith("T5 documentation")
