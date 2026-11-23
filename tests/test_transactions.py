"""Transaction boundaries: one transaction per business operation, no external calls inside one."""
import asyncio
import re
import threading
from pathlib import Path

import httpx
import pytest

from swarmforge import runqueue
from swarmforge.db import Store, TransactionViolation, outside_transaction
from swarmforge.projects import service
from swarmforge.projects.store import Projects

from .conftest import FakeProvider
from .test_lead_actions import definition

SRC = Path(__file__).resolve().parents[1] / "src" / "swarmforge"


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


def test_the_guard_applies_only_to_the_thread_holding_the_transaction(store):
    outside_transaction("A call")  # no transaction: fine
    seen = []
    with store.transaction():
        with pytest.raises(TransactionViolation):
            outside_transaction("A model call")
        t = threading.Thread(target=lambda: seen.append(outside_transaction("Another thread's call")))
        t.start()
        t.join()
    assert seen == [None]
    outside_transaction("After commit")


def test_model_calls_refuse_to_run_inside_a_transaction(cfg, store):
    from swarmforge.llm import LLMClient
    fake = FakeProvider({"qwen-fake": lambda body: {"content": "hi"}})
    llm = LLMClient(cfg, store=store, run_id=None, transport=fake.transport)
    with store.transaction():
        with pytest.raises(TransactionViolation):
            asyncio.run(llm.chat("planner", [{"role": "user", "content": "x"}]))
    assert fake.requests == []  # nothing left the machine
    assert asyncio.run(llm.chat("planner", [{"role": "user", "content": "x"}])).message["content"] == "hi"


def test_github_calls_and_pushes_refuse_to_run_inside_a_transaction(cfg, store, tmp_path):
    from swarmforge.github import GitHubClient, push_branch
    sent = []
    client = GitHubClient(cfg.github, transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, json={})))
    with store.transaction():
        with pytest.raises(TransactionViolation):
            client._client.get("/repos/o/r")
        with pytest.raises(TransactionViolation):
            push_branch(tmp_path, "o", "r", "swarmforge/x", "token", "main", "swarmforge/")
    assert sent == []


def _running_project(store):
    ps = Projects(store)
    p = ps.create()
    ps.draft(p["id"], definition())
    service.approve(store, p["id"])
    return ps, p["id"]


def test_project_cancel_is_all_or_nothing(store, monkeypatch):
    ps, pid = _running_project(store)
    for key in ("a", "b"):
        store.queue_add(key, "body", "owner/inventory", project_id=pid, milestone=0, project_key=key, queue=service.lane(pid))
    running = store.queue_add("c", "body", "owner/inventory", project_id=pid, milestone=0, project_key="c", queue=service.lane(pid))
    store.create_run("run-c", repo="owner/inventory", issue_title="c")
    store.queue_update(running, status="running", run_id="run-c")

    def broken(run_id):
        raise RuntimeError("disk full")
    monkeypatch.setattr(store, "request_cancel", broken)
    with pytest.raises(RuntimeError):
        service.cancel(store, pid)
    assert ps.get(pid)["status"] == "running"
    assert sorted(q["status"] for q in ps.items(pid)) == ["queued", "queued", "running"]  # nothing half-cancelled
    monkeypatch.undo()
    service.cancel(store, pid)
    assert ps.get(pid)["status"] == "cancelled" and store.get_run("run-c")["cancel_requested"]


def test_resume_reopens_the_lane_in_the_same_commit(store, monkeypatch):
    ps, pid = _running_project(store)
    service.pause(store, pid)
    runqueue.set_lane_paused(store, service.lane(pid), True)
    original = store.conn.execute

    def fail_on_event(sql, *args):
        if sql.startswith("INSERT INTO project_events") and "reply" in str(args):
            raise RuntimeError("crash before commit")
        return original(sql, *args)

    class Conn:  # sqlite3 connections cannot be monkeypatched directly
        def __getattr__(self, name):
            return getattr(store.__dict__["_real"], name)

        def execute(self, sql, *args):
            return fail_on_event(sql, *args)
    store.__dict__["_real"] = store.conn
    monkeypatch.setattr(store, "conn", Conn())
    with pytest.raises(RuntimeError):
        service.resume(store, pid, "continue")
    monkeypatch.undo()
    assert ps.get(pid)["status"] == "waiting_for_you"
    assert store.kv_get(runqueue.LANE_PAUSED.format(service.lane(pid))) == "1"  # still paused: nothing committed


def test_two_retries_of_the_same_item_add_one_copy(cfg, tmp_path):
    first = Store(cfg.db_path)
    item_id = first.queue_add("Flaky task", "body", "owner/app")
    first.queue_update(item_id, status="failed")
    item = first.queue_item(item_id)
    results = []

    def retry():
        s = Store(cfg.db_path)
        try:
            results.append(runqueue.requeue(s, item))
        finally:
            s.close()
    threads = [threading.Thread(target=retry) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(created for _, created in results) == 1 and len({i for i, _ in results}) == 1
    assert len([q for q in first.queue_items() if q["title"] == "Flaky task" and q["status"] == "queued"]) == 1
    first.close()


# Opening a write transaction by hand bypasses Store.transaction(): helpers inside it commit halfway
# and nothing stops an external call while the write lock is held. Read snapshots on the web
# server's own read-only connections ("BEGIN" alone) are fine.
ALLOWED = {"db.py", "execution.py"}  # the implementation itself, and the fallback for non-Store objects
PATTERNS = [r"BEGIN IMMEDIATE", r"with\s+\w+(\.store)?\.conn\s*:", r"_lock,\s*\w+(\.store)?\.conn\b", r"\.conn\.commit\(\)"]


def test_no_hand_rolled_write_transactions_outside_the_store():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in PATTERNS:
            for m in re.finditer(pattern, text):
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(SRC)}:{line}: {m.group(0)}")
    # jobs.ensure_tables commits its schema script only outside a transaction (it checks first).
    offenders = [o for o in offenders if not o.startswith("jobs.py:") or ".conn.commit()" not in o]
    assert offenders == [], "Use Store.transaction() instead:\n" + "\n".join(offenders)
