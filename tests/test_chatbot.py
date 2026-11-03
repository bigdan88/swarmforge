"""Offline tests for the status chatbot (swarmforge.chatbot)."""
import time

import pytest

from swarmforge.chatbot import ChatError, answer, ask, resolve_role, status_snapshot
from swarmforge.config import parse_config
from swarmforge.db import Store

from .conftest import FakeProvider, base_raw


def _make_run(store: Store, run_id: str, **overrides) -> None:
    now = time.time()
    fields = {
        "created_at": now, "updated_at": now, "finished_at": None, "mode": "swarm",
        "issue_url": "https://github.com/o/r/issues/1", "issue_title": "Add export",
        "repo": "o/r", "base_branch": "main", "integration_branch": "swarmforge/issue-1-abc",
        "run_dir": "/tmp/runs/r1", "state": "COMPLETED", "state_reason": "done",
        "repair_cycle": 1, "budget_usd": 20.0, "pr_url": None, "cancel_requested": 0,
        "plan_json": None, "config_json": None, "pid": None, **overrides,
    }
    store.create_run(run_id, **fields)


def _make_task(store: Store, run_id: str, task_key: str, **overrides) -> None:
    fields = {
        "cycle": 0, "kind": "implement", "title": f"Task {task_key}", "description": "desc",
        "role": "developer_backend", "model": "deepseek-fake", "depends_on": [],
        "acceptance_criteria": ["x"], "expected_files": [f"{task_key}.py"],
        "state": "INTEGRATED", "branch": f"swarmforge/{task_key}", "worktree": f"/tmp/wt/{task_key}",
        "started_at": time.time() - 100, "ended_at": time.time() - 50, "attempts": 1,
        "summary": "done", "error": None, **overrides,
    }
    store.upsert_task(run_id, task_key, **fields)


@pytest.fixture
def seeded_store(tmp_path) -> Store:
    store = Store(tmp_path / "db.sqlite")
    _make_run(store, "run-competed-1234", state="COMPLETED", state_reason="done",
              finished_at=time.time(), created_at=time.time() - 200)
    _make_task(store, "run-competed-1234", "T1", state="SUCCEEDED")
    _make_task(store, "run-competed-1234", "T2", state="INTEGRATED")
    _make_run(store, "run-failed-5678", state="FAILED", state_reason="tests failed",
              finished_at=time.time(), created_at=time.time() - 100)
    _make_task(store, "run-failed-5678", "T1", state="FAILED", error="ImportError: no module x")
    store.failure_save("run-failed-5678", title="Add export", repo="o/r", state="FAILED",
                       category="agent_failure", root_cause="developer task failed",
                       outcome="retry", retry=1)
    store.queue_add("Fix login", "body", "o/r")
    return store


# ------------------------------------------------------------- status_snapshot

def test_snapshot_covers_runs_tasks_failures_queue(seeded_store):
    snap = status_snapshot(seeded_store)
    assert "run-comp" in snap and "COMPLETED" in snap
    assert "run-fail" in snap and "FAILED" in snap
    assert "[SUCCEEDED] T1" in snap and "[INTEGRATED] T2" in snap
    assert "[FAILED] T1" in snap and "ImportError" in snap
    assert "agent_failure" in snap
    assert "Fix login" in snap
    assert "Pending approvals: 0" in snap


def test_snapshot_empty_store(tmp_path):
    store = Store(tmp_path / "empty.sqlite")
    snap = status_snapshot(store)
    assert "No runs yet" in snap


def test_snapshot_capped(tmp_path):
    store = Store(tmp_path / "big.sqlite")
    for i in range(30):
        _make_run(store, f"run-{i:04d}-aaaaaaaaaaaa", issue_title="x" * 400)
    snap = status_snapshot(store, max_runs=30)
    assert len(snap) <= 14_200
    assert "truncated" in snap


# ------------------------------------------------------------------- resolve_role

def test_resolve_role_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    cfg = parse_config(base_raw(tmp_path / "work"))
    assert resolve_role(cfg) == "planner"


def test_resolve_role_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["roles"]["chat"] = {"provider": "fake", "model": "qwen-fake"}
    cfg = parse_config(raw)
    assert resolve_role(cfg) == "chat"


# ----------------------------------------------------------------------- answer

def _fake():
    return FakeProvider({"qwen-fake": lambda body: {"role": "assistant",
                                                     "content": "2 runs: 1 completed, 1 failed"}})


def test_answer_sends_snapshot_and_history(seeded_store, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    cfg = parse_config(base_raw(tmp_path / "work"))
    fake = _fake()
    res = ask(cfg, seeded_store, "How many runs?", transport=fake.transport)
    assert res == {"answer": "2 runs: 1 completed, 1 failed", "role": "planner",
                   "model": "qwen-fake", "question": "How many runs?", "actions": {"tasks": [], "fixes": []},
                   "tools_used": []}
    msgs = fake.requests[0]["messages"]
    assert "<status>" in msgs[0]["content"] and "run-comp" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "How many runs?"}

    fake2 = _fake()
    ask(cfg, seeded_store, "And how many failed?", history=[
        {"role": "user", "content": "How many runs?"},
        {"role": "assistant", "content": "2 runs: 1 completed, 1 failed"}], transport=fake2.transport)
    msgs2 = fake2.requests[0]["messages"]
    assert msgs2[1] == {"role": "user", "content": "How many runs?"}
    assert msgs2[2] == {"role": "assistant", "content": "2 runs: 1 completed, 1 failed"}
    assert msgs2[-1]["content"] == "And how many failed?"


def test_answer_async(seeded_store, tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("FAKE_KEY", "k")
    cfg = parse_config(base_raw(tmp_path / "work"))
    fake = _fake()
    res = asyncio.run(answer(cfg, seeded_store, "status?", transport=fake.transport))
    assert res["answer"] == "2 runs: 1 completed, 1 failed"


def test_ask_missing_key_is_chat_error(seeded_store, tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_KEY", raising=False)
    cfg = parse_config(base_raw(tmp_path / "work"))
    with pytest.raises(ChatError):
        ask(cfg, seeded_store, "hi?")
