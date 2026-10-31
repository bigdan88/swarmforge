"""Acceptance tests for the --json output feature of `swarmforge runs` and `swarmforge status`.

These tests verify the acceptance criteria from the issue:
  AC1: `runs --json` output parses as JSON and contains the listed fields with correct types.
  AC2: `status --json` includes the task list and the extra fields; unknown run gives the
       error object and exit code 2.
  AC3: Text output without `--json` is unchanged and the existing test suite still passes.
  AC4: README documents both flags.

The bulk of the unit-level coverage already lives in tests/test_cli_json.py. This file adds
focused acceptance-level checks that exercise the full CLI pipeline end-to-end and verify
the documentation requirement.
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
from pathlib import Path

import pytest
import yaml

from swarmforge.cli import main
from swarmforge.db import Store

from .conftest import base_raw

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"


# ---------------------------------------------------------------- shared helpers
def _write_cfg(tmp_path, monkeypatch) -> Path:
    """Write a minimal config file and return its path."""
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    return p


def _capture_stdout(argv: list[str]) -> tuple[str, int]:
    """Run main(argv) and return (captured_stdout, exit_code)."""
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
        "issue_number": 1,
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


# ===================================================================== AC1
# `runs --json` output parses as JSON and contains the listed fields with correct types.
class TestRunsJsonFields:
    """AC1: runs --json output is a JSON array with the documented fields and types."""

    def test_runs_json_is_array(self, tmp_path, monkeypatch):
        """The top-level value must be a JSON array (not an object)."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001")

        out, code = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
        assert code == 0
        data = json.loads(out)
        assert isinstance(data, list), "runs --json must output a JSON array"

    def test_runs_json_all_required_fields_present(self, tmp_path, monkeypatch):
        """Every documented field must be present in each run object."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001")

        out, _ = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
        data = json.loads(out)
        required = {
            "id", "mode", "state", "state_reason", "issue_title", "issue_url",
            "repo", "created_at", "finished_at", "cost_usd", "total_tokens",
        }
        assert required <= set(data[0].keys()), (
            f"missing fields: {required - set(data[0].keys())}"
        )

    def test_runs_json_field_types(self, tmp_path, monkeypatch):
        """Each field must have the correct JSON type."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        now = time.time()
        _make_run(store, "run-001", created_at=now, finished_at=now + 60)
        _make_llm_call(store, "run-001", cost_usd=0.005, total_tokens=700)

        out, _ = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
        r = json.loads(out)[0]

        assert isinstance(r["id"], str)
        assert isinstance(r["mode"], str)
        assert isinstance(r["state"], str)
        assert isinstance(r["state_reason"], str)
        assert isinstance(r["issue_title"], str)
        assert isinstance(r["issue_url"], str)
        assert isinstance(r["repo"], str)
        assert isinstance(r["created_at"], float), "created_at must be a Unix epoch float"
        assert isinstance(r["finished_at"], float), "finished_at must be a Unix epoch float"
        assert isinstance(r["cost_usd"], float), "cost_usd must be a float"
        assert isinstance(r["total_tokens"], int), "total_tokens must be an int"

    def test_runs_json_timestamps_are_epoch_floats(self, tmp_path, monkeypatch):
        """Timestamps must be Unix epoch floats exactly as stored (not ISO strings)."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        now = time.time()
        _make_run(store, "run-001", created_at=now, finished_at=now + 60)

        out, _ = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
        r = json.loads(out)[0]

        # Must be numeric, not a string
        assert not isinstance(r["created_at"], str)
        assert not isinstance(r["finished_at"], str)
        # Must be close to the stored epoch value
        assert r["created_at"] == pytest.approx(now, abs=1)
        assert r["finished_at"] == pytest.approx(now + 60, abs=1)

    def test_runs_json_finished_at_null_while_running(self, tmp_path, monkeypatch):
        """finished_at must be JSON null while the run is still running."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-002", state="TASK_EXECUTION", finished_at=None)

        out, _ = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
        r = json.loads(out)[0]
        assert r["finished_at"] is None

    def test_runs_json_limit_works_with_json(self, tmp_path, monkeypatch):
        """--limit must keep working alongside --json."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        for i in range(5):
            _make_run(store, f"run-{i:03d}", created_at=float(1000 + i))

        out, _ = _capture_stdout(["--config", str(cfg_path), "runs", "--json", "--limit", "2"])
        data = json.loads(out)
        assert len(data) == 2
        # most recent first
        assert data[0]["id"] == "run-004"
        assert data[1]["id"] == "run-003"

    def test_runs_json_cost_aggregates_across_calls(self, tmp_path, monkeypatch):
        """cost_usd must be the total known cost across all LLM calls."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001")
        _make_llm_call(store, "run-001", cost_usd=0.005, total_tokens=700, cost_source="provider_reported")
        _make_llm_call(store, "run-001", cost_usd=0.003, total_tokens=300, cost_source="estimated")

        out, _ = _capture_stdout(["--config", str(cfg_path), "runs", "--json"])
        r = json.loads(out)[0]
        assert r["cost_usd"] == pytest.approx(0.008)
        assert r["total_tokens"] == 1000


# ===================================================================== AC2
# `status --json` includes the task list and the extra fields; unknown run gives the
# error object and exit code 2.
class TestStatusJsonFields:
    """AC2: status --json output is a JSON object with tasks and extra fields."""

    def test_status_json_is_object(self, tmp_path, monkeypatch):
        """The top-level value must be a JSON object (not an array)."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001")

        out, code = _capture_stdout(["--config", str(cfg_path), "status", "run-001", "--json"])
        assert code == 0
        data = json.loads(out)
        assert isinstance(data, dict), "status --json must output a JSON object"

    def test_status_json_all_required_fields_present(self, tmp_path, monkeypatch):
        """Every documented field must be present in the status object."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001")

        out, _ = _capture_stdout(["--config", str(cfg_path), "status", "run-001", "--json"])
        data = json.loads(out)
        required = {
            "id", "mode", "state", "state_reason", "issue_title", "issue_url",
            "repo", "created_at", "finished_at", "cost_usd", "total_tokens",
            "integration_branch", "pr_url", "repair_cycle", "interventions", "tasks",
        }
        assert required <= set(data.keys()), (
            f"missing fields: {required - set(data.keys())}"
        )

    def test_status_json_extra_fields_have_correct_values(self, tmp_path, monkeypatch):
        """The extra fields (integration_branch, pr_url, repair_cycle, interventions) must
        reflect the stored values."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001",
                  integration_branch="swarmforge/issue-1-abc",
                  pr_url="https://github.com/o/r/pull/7",
                  repair_cycle=2)
        _make_intervention(store, "run-001", "push_approved")
        _make_intervention(store, "run-001", "cancel")
        _make_intervention(store, "run-001", "merge")

        out, _ = _capture_stdout(["--config", str(cfg_path), "status", "run-001", "--json"])
        data = json.loads(out)
        assert data["integration_branch"] == "swarmforge/issue-1-abc"
        assert data["pr_url"] == "https://github.com/o/r/pull/7"
        assert data["repair_cycle"] == 2
        assert data["interventions"] == 3

    def test_status_json_tasks_list_structure(self, tmp_path, monkeypatch):
        """Each task object must contain the documented fields."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001")
        _make_task(store, "run-001", "T1",
                   kind="implement", cycle=0, role="developer_backend",
                   model="deepseek-fake", state="INTEGRATED", depends_on=[])
        _make_task(store, "run-001", "T2",
                   kind="test", cycle=1, role="tester",
                   model="minimax-fake", state="RUNNING", depends_on=["T1"])

        out, _ = _capture_stdout(["--config", str(cfg_path), "status", "run-001", "--json"])
        data = json.loads(out)
        assert isinstance(data["tasks"], list)
        assert len(data["tasks"]) == 2

        task_required = {"task_key", "kind", "cycle", "title", "role", "model", "state", "depends_on"}
        for t in data["tasks"]:
            assert task_required <= set(t.keys()), (
                f"task missing fields: {task_required - set(t.keys())}"
            )

        t1, t2 = data["tasks"]
        assert t1["task_key"] == "T1"
        assert t1["kind"] == "implement"
        assert t1["cycle"] == 0
        assert t1["role"] == "developer_backend"
        assert t1["model"] == "deepseek-fake"
        assert t1["state"] == "INTEGRATED"
        assert t1["depends_on"] == []

        assert t2["task_key"] == "T2"
        assert t2["kind"] == "test"
        assert t2["cycle"] == 1
        assert t2["role"] == "tester"
        assert t2["model"] == "minimax-fake"
        assert t2["state"] == "RUNNING"
        assert t2["depends_on"] == ["T1"]

    def test_status_json_unknown_run_error_object(self, tmp_path, monkeypatch):
        """Unknown run id must print exactly {"error": "run not found: <id>"}."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        out, code = _capture_stdout(["--config", str(cfg_path), "status", "nonexistent", "--json"])
        assert code == 2
        data = json.loads(out)
        assert data == {"error": "run not found: nonexistent"}

    def test_status_json_unknown_run_exit_code_2(self, tmp_path, monkeypatch):
        """Unknown run id must exit with status code 2."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        _, code = _capture_stdout(["--config", str(cfg_path), "status", "nonexistent", "--json"])
        assert code == 2

    def test_status_json_finished_at_null(self, tmp_path, monkeypatch):
        """finished_at must be JSON null for a running run."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001", state="TASK_EXECUTION", finished_at=None)

        out, _ = _capture_stdout(["--config", str(cfg_path), "status", "run-001", "--json"])
        data = json.loads(out)
        assert data["finished_at"] is None

    def test_status_json_empty_tasks_list(self, tmp_path, monkeypatch):
        """A run with no tasks must still include an empty tasks list."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001")

        out, _ = _capture_stdout(["--config", str(cfg_path), "status", "run-001", "--json"])
        data = json.loads(out)
        assert data["tasks"] == []
        assert data["interventions"] == 0


# ===================================================================== AC3
# Text output without --json is unchanged and the existing test suite still passes.
class TestTextOutputUnchanged:
    """AC3: Without --json, the existing text output must stay exactly the same."""

    def test_runs_text_output_has_header_and_rows(self, tmp_path, monkeypatch):
        """runs text output must contain the header columns and the run row."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001")

        out, code = _capture_stdout(["--config", str(cfg_path), "runs"])
        assert code == 0
        # Must NOT be valid JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)
        # Must contain the header columns
        assert "RUN ID" in out
        assert "MODE" in out
        assert "STATE" in out
        assert "COST" in out
        assert "STARTED" in out
        assert "ISSUE" in out
        # Must contain the run id
        assert "run-001" in out

    def test_runs_text_output_empty_store(self, tmp_path, monkeypatch):
        """runs text output on an empty store must print 'no runs yet'."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        out, code = _capture_stdout(["--config", str(cfg_path), "runs"])
        assert code == 0
        assert "no runs yet" in out

    def test_status_text_output_has_expected_sections(self, tmp_path, monkeypatch):
        """status text output must contain the expected sections."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        store = Store(Path(cfg_path).parent / "work" / "swarmforge.db")
        _make_run(store, "run-001")
        _make_task(store, "run-001", "T1")

        out, code = _capture_stdout(["--config", str(cfg_path), "status", "run-001"])
        assert code == 0
        # Must NOT be valid JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)
        # Must contain expected sections
        assert "Run" in out
        assert "run-001" in out
        assert "swarm" in out
        assert "Issue" in out
        assert "State" in out
        assert "Cost" in out
        assert "Tasks:" in out
        assert "T1" in out

    def test_status_text_output_unknown_run(self, tmp_path, monkeypatch):
        """status text output for an unknown run must print the error and exit 2."""
        cfg_path = _write_cfg(tmp_path, monkeypatch)
        out, code = _capture_stdout(["--config", str(cfg_path), "status", "nonexistent"])
        assert code == 2
        assert "run not found: nonexistent" in out


# ===================================================================== AC4
# README documents both flags.
class TestReadmeDocumentation:
    """AC4: README.md must document both --json flags."""

    @pytest.fixture
    def readme_text(self) -> str:
        return README_PATH.read_text(encoding="utf-8")

    def test_readme_documents_runs_json(self, readme_text):
        """README must document `swarmforge runs --json`."""
        # Look for a mention of `runs --json` or `runs --json` in the Use section
        assert "runs --json" in readme_text, (
            "README.md must document `swarmforge runs --json`"
        )

    def test_readme_documents_status_json(self, readme_text):
        """README must document `swarmforge status --json`."""
        assert "status" in readme_text and "--json" in readme_text, (
            "README.md must document `swarmforge status --json`"
        )

    def test_readme_documents_status_json_error_behavior(self, readme_text):
        """README must document the error behavior for unknown run ids."""
        # The issue says: "Unknown run id: print {"error": "run not found: <id>"} and exit with status 2."
        assert "run not found" in readme_text, (
            "README.md must document the 'run not found' error behavior"
        )

    def test_readme_documents_status_json_extra_fields(self, readme_text):
        """README must mention the extra fields available in status --json."""
        # The issue lists: integration_branch, pr_url, repair_cycle, interventions, tasks
        for field in ("integration_branch", "pr_url", "repair_cycle", "interventions", "tasks"):
            assert field in readme_text, (
                f"README.md must mention the '{field}' field in status --json documentation"
            )

    def test_readme_documents_runs_json_fields(self, readme_text):
        """README must mention the fields available in runs --json."""
        # The issue lists: id, mode, state, cost_usd, total_tokens
        for field in ("cost_usd", "total_tokens"):
            assert field in readme_text, (
                f"README.md must mention the '{field}' field in runs --json documentation"
            )

    def test_readme_documentation_in_use_section(self, readme_text):
        """The --json documentation must be in the Use section of the README."""
        # Find the Use section
        use_match = re.search(r"^## Use\s*$", readme_text, re.MULTILINE)
        assert use_match, "README.md must have a '## Use' section"
        use_start = use_match.end()
        # Find the next ## section
        next_section = re.search(r"^## ", readme_text[use_start:], re.MULTILINE)
        use_end = use_start + next_section.start() if next_section else len(readme_text)
        use_section = readme_text[use_start:use_end]

        assert "runs --json" in use_section, (
            "`runs --json` must be documented in the Use section"
        )
        assert "status" in use_section and "--json" in use_section, (
            "`status --json` must be documented in the Use section"
        )
