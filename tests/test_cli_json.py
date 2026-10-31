"""Unit tests for --json output of `swarmforge runs` and `swarmforge status`."""
import io
import json
import sys
import time
from pathlib import Path

import pytest
import yaml

from swarmforge.cli import main
from swarmforge.db import Store

from .conftest import base_raw


def _write_cfg(tmp_path, monkeypatch) -> Path:
    """Write a minimal config file and return its path."""
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    return p


def _capture_stdout(argv: list[str]) -> str:
    """Run main(argv) and return captured stdout as a string."""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        exit_code = main(argv)
    finally:
        sys.stdout = old
    return buf.getvalue(), exit_code


def _make_run(store: Store, run_id: str, **overrides) -> dict:
    """Create a run with sensible defaults and return the stored row."""
    now = time.time()
    fields = {
        "id": run_id,
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
        "mode": "swarm",
        "issue_url": "https://github.com/o/r/issues/1",
        "issue_title": "Add export",
        "repo": "o/r",
        "base_branch": "main",
        "integration_branch": "swarmforge/issue-1-abc",
        "run_dir": "/tmp/runs/r1",
        "state": "COMPLETED",
        "state_reason": "done",
        "repair_cycle": 1,
        "budget_usd": 20.0,
        "pr_url": "https://github.com/o/r/pull/7",
        "cancel_requested": 0,
        "plan_json": None,
        "config_json": None,
        "pid": None,
        **overrides,
    }
    store.create_run(run_id, **{k: v for k, v in fields.items() if k != "id"})
    return store.get_run(run_id)


def _make_task(store: Store, run_id: str, task_key: str, **overrides) -> None:
    """Create a task with sensible defaults."""
    fields = {
        "cycle": 0,
        "kind": "implement",
        "title": f"Task {task_key}",
        "description": "desc",
        "role": "developer_backend",
        "model": "deepseek-fake",
        "depends_on": [],
        "acceptance_criteria": ["x"],
        "expected_files": [f"{task_key}.py"],
        "state": "INTEGRATED",
        "branch": f"swarmforge/{task_key}",
        "worktree": f"/tmp/wt/{task_key}",
        "started_at": time.time() - 100,
        "ended_at": time.time() - 50,
        "attempts": 1,
        "summary": "done",
        "error": None,
        **overrides,
    }
    store.upsert_task(run_id, task_key, **fields)


def _make_llm_call(store: Store, run_id: str, **overrides) -> None:
    """Record an LLM call with sensible defaults."""
    fields = {
        "task_key": "T1",
        "role": "developer_backend",
        "provider": "fake",
        "model": "deepseek-fake",
        "started_at": time.time() - 100,
        "ended_at": time.time() - 99,
        "prompt_tokens": 500,
        "completion_tokens": 200,
        "total_tokens": 700,
        "cached_tokens": 0,
        "cost_usd": 0.001,
        "cost_source": "provider_reported",
        "status": "ok",
        "error": None,
        **overrides,
    }
    store.record_llm_call(run_id, **fields)


def _make_intervention(store: Store, run_id: str, kind: str = "push_approved", detail: str = "") -> None:
    """Record an intervention."""
    store.record_intervention(run_id, kind, detail)


# ===================================================================== runs --json

def test_runs_json_empty(tmp_path, monkeypatch):
    """runs --json on an empty store prints an empty JSON array."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    out, code = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data == []


def test_runs_json_single_run(tmp_path, monkeypatch):
    """runs --json prints one object per run with correct fields and types."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    now = time.time()
    _make_run(store, "run-001", created_at=now, finished_at=now + 60)
    _make_llm_call(store, "run-001", cost_usd=0.005, total_tokens=700)
    _make_llm_call(store, "run-001", cost_usd=0.003, total_tokens=300, cost_source="estimated")

    out, code = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
    assert code == 0
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 1

    r = data[0]
    assert r["id"] == "run-001"
    assert r["mode"] == "swarm"
    assert r["state"] == "COMPLETED"
    assert r["state_reason"] == "done"
    assert r["issue_title"] == "Add export"
    assert r["issue_url"] == "https://github.com/o/r/issues/1"
    assert r["repo"] == "o/r"
    assert isinstance(r["created_at"], float)
    assert r["created_at"] == pytest.approx(now, abs=1)
    assert isinstance(r["finished_at"], float)
    assert r["finished_at"] == pytest.approx(now + 60, abs=1)
    assert isinstance(r["cost_usd"], float)
    assert r["cost_usd"] == pytest.approx(0.008)
    assert isinstance(r["total_tokens"], int)
    assert r["total_tokens"] == 1000


def test_runs_json_finished_at_null_while_running(tmp_path, monkeypatch):
    """finished_at is null when the run is still running."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    _make_run(store, "run-002", state="TASK_EXECUTION", finished_at=None)

    out, code = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data[0]["finished_at"] is None


def test_runs_json_multiple_runs(tmp_path, monkeypatch):
    """runs --json returns multiple runs ordered by created_at DESC."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    _make_run(store, "run-old", created_at=1000.0)
    _make_run(store, "run-new", created_at=2000.0)

    out, code = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
    assert code == 0
    data = json.loads(out)
    assert len(data) == 2
    assert data[0]["id"] == "run-new"
    assert data[1]["id"] == "run-old"


def test_runs_json_with_limit(tmp_path, monkeypatch):
    """runs --json --limit N returns at most N runs."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    for i in range(5):
        _make_run(store, f"run-{i:03d}", created_at=float(1000 + i))

    out, code = _capture_stdout(["--config", str(cfg_path), "runs", "--json", "--limit", "3"])
    assert code == 0
    data = json.loads(out)
    assert len(data) == 3
    # most recent first
    assert data[0]["id"] == "run-004"


def test_runs_json_cost_zero_when_no_calls(tmp_path, monkeypatch):
    """cost_usd and total_tokens are 0 when there are no LLM calls."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    _make_run(store, "run-003")

    out, code = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data[0]["cost_usd"] == 0.0
    assert data[0]["total_tokens"] == 0


# =================================================================== status --json

def test_status_json_full(tmp_path, monkeypatch):
    """status --json prints one JSON object with all fields including tasks."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    now = time.time()
    _make_run(store, "run-001", created_at=now, finished_at=now + 120)
    _make_llm_call(store, "run-001", cost_usd=0.01, total_tokens=1000)
    _make_intervention(store, "run-001", "push_approved")
    _make_intervention(store, "run-001", "cancel")
    _make_task(store, "run-001", "T1", depends_on=[], role="developer_backend", model="deepseek-fake")
    _make_task(store, "run-001", "T2", depends_on=[], role="developer_frontend", model="kimi-fake")
    _make_task(store, "run-001", "T3", depends_on=["T1", "T2"], role="developer_backend", model="deepseek-fake",
               state="RUNNING")

    out, code = _capture_stdout(["--config", str(cfg_path), "status", "run-001", "--json"])
    assert code == 0
    data = json.loads(out)

    # top-level fields
    assert data["id"] == "run-001"
    assert data["mode"] == "swarm"
    assert data["state"] == "COMPLETED"
    assert data["state_reason"] == "done"
    assert data["issue_title"] == "Add export"
    assert data["issue_url"] == "https://github.com/o/r/issues/1"
    assert data["repo"] == "o/r"
    assert isinstance(data["created_at"], float)
    assert isinstance(data["finished_at"], float)
    assert isinstance(data["cost_usd"], float)
    assert data["cost_usd"] == pytest.approx(0.01)
    assert isinstance(data["total_tokens"], int)
    assert data["total_tokens"] == 1000
    assert data["integration_branch"] == "swarmforge/issue-1-abc"
    assert data["pr_url"] == "https://github.com/o/r/pull/7"
    assert data["repair_cycle"] == 1
    assert data["interventions"] == 2

    # tasks
    assert isinstance(data["tasks"], list)
    assert len(data["tasks"]) == 3
    t1 = data["tasks"][0]
    assert t1["task_key"] == "T1"
    assert t1["kind"] == "implement"
    assert t1["cycle"] == 0
    assert t1["title"] == "Task T1"
    assert t1["role"] == "developer_backend"
    assert t1["model"] == "deepseek-fake"
    assert t1["state"] == "INTEGRATED"
    assert t1["depends_on"] == []

    t3 = data["tasks"][2]
    assert t3["task_key"] == "T3"
    assert t3["depends_on"] == ["T1", "T2"]
    assert t3["state"] == "RUNNING"


def test_status_json_unknown_run(tmp_path, monkeypatch):
    """status --json with unknown run id returns error object and exit code 2."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    # Don't create any runs
    out, code = _capture_stdout(["--config", str(cfg_path), "status", "nonexistent", "--json"])
    assert code == 2
    data = json.loads(out)
    assert data == {"error": "run not found: nonexistent"}


def test_status_json_latest_run(tmp_path, monkeypatch):
    """status --json with no run_id (defaults to 'latest') works."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    _make_run(store, "run-latest", created_at=time.time())

    out, code = _capture_stdout(["--config", str(cfg_path), "status", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data["id"] == "run-latest"


def test_status_json_no_tasks(tmp_path, monkeypatch):
    """status --json with a run that has no tasks still includes an empty tasks list."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    _make_run(store, "run-001")

    out, code = _capture_stdout(["--config", str(cfg_path), "status", "run-001", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data["tasks"] == []
    assert data["interventions"] == 0


def test_status_json_finished_at_null(tmp_path, monkeypatch):
    """finished_at is null for a running run in status --json."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    _make_run(store, "run-001", state="TASK_EXECUTION", finished_at=None)

    out, code = _capture_stdout(["--config", str(cfg_path), "status", "run-001", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data["finished_at"] is None


# ===================================================== text output unchanged

def test_runs_text_output_no_json_flag(tmp_path, monkeypatch):
    """Without --json, runs prints human-readable text (not JSON)."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    _make_run(store, "run-001")

    out, code = _capture_stdout(["--config", str(cfg_path), "runs"])
    assert code == 0
    # Should not be valid JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    # Should contain the run id and header
    assert "run-001" in out
    assert "RUN ID" in out
    assert "MODE" in out


def test_runs_text_output_empty(tmp_path, monkeypatch):
    """Without --json, runs on empty store prints 'no runs yet'."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    out, code = _capture_stdout(["--config", str(cfg_path), "runs"])
    assert code == 0
    assert "no runs yet" in out


def test_status_text_output_no_json_flag(tmp_path, monkeypatch):
    """Without --json, status prints human-readable text (not JSON)."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    _make_run(store, "run-001")
    _make_task(store, "run-001", "T1")

    out, code = _capture_stdout(["--config", str(cfg_path), "status", "run-001"])
    assert code == 0
    # Should not be valid JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    # Should contain expected text
    assert "Run" in out
    assert "run-001" in out
    assert "swarm" in out
    assert "Issue" in out
    assert "State" in out
    assert "Cost" in out
    assert "Tasks:" in out
    assert "T1" in out


def test_status_text_output_unknown_run(tmp_path, monkeypatch):
    """Without --json, status with unknown run prints error and exits 2."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    out, code = _capture_stdout(["--config", str(cfg_path), "status", "nonexistent"])
    assert code == 2
    assert "run not found: nonexistent" in out


def test_runs_json_output_is_valid_json_on_stdout_only(tmp_path, monkeypatch):
    """runs --json output is pure JSON with a trailing newline, nothing else."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    _make_run(store, "run-001")

    out, code = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
    assert code == 0
    # Should parse as JSON
    data = json.loads(out)
    assert isinstance(data, list)
    # Output should end with newline (json.dump adds none, but the code adds sys.stdout.write("\n"))
    assert out.endswith("\n")


def test_status_json_output_is_valid_json_on_stdout_only(tmp_path, monkeypatch):
    """status --json output is pure JSON, nothing else."""
    cfg_path = _write_cfg(tmp_path, monkeypatch)
    store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
    _make_run(store, "run-001")

    out, code = _capture_stdout(["--config", str(cfg_path), "status", "run-001", "--json"])
    assert code == 0
    data = json.loads(out)
    assert isinstance(data, dict)
    assert data["id"] == "run-001"
