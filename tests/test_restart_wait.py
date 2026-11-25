"""A pending worker restart must not make an idle project look stuck."""
from swarmforge import runqueue
from swarmforge.db import Store
from swarmforge.projects import service
from swarmforge.projects.store import Projects

from .test_projects import definition


def _running_project(store, name):
    ps = Projects(store)
    p = ps.create(name)
    spec = definition()
    spec["repo"] = "acme/" + name.lower()
    ps.draft(p["id"], spec)
    service.approve(store, p["id"])
    ps.update(p["id"], status="running")
    return p["id"]


def test_idle_project_explains_the_restart_wait_and_clears_it(cfg):
    store = Store(cfg.db_path)
    try:
        idle = _running_project(store, "Idle")
        busy = _running_project(store, "Busy")
        store.kv_set("project_activity:" + busy, "Building the swipe deck")
        running = [{"id": 110, "title": "Add image tool", "project_id": None},
                   {"id": 116, "title": "Probe durations", "project_id": busy}]

        runqueue.note_restart_wait(store, running)

        text = store.kv_get("project_activity:" + idle)
        assert text.startswith(runqueue.RESTART_WAIT_PREFIX)
        assert "#110 Add image tool" in text and "#116 Probe durations" in text
        assert store.kv_get("project_activity:" + busy) == "Building the swipe deck"  # its own work, untouched

        runqueue.note_restart_wait(store, [])  # restart happened
        assert not store.kv_get("project_activity:" + idle)
        assert store.kv_get("project_activity:" + busy) == "Building the swipe deck"
    finally:
        store.close()


def test_other_activity_is_left_alone_when_no_restart_is_pending(cfg):
    store = Store(cfg.db_path)
    try:
        pid = _running_project(store, "Lead")
        store.kv_set("project_activity:" + pid, "Recovering automatically: replanning")
        runqueue.note_restart_wait(store, [])
        assert store.kv_get("project_activity:" + pid) == "Recovering automatically: replanning"
    finally:
        store.close()


async def test_restart_hands_over_instead_of_making_everyone_wait(cfg, monkeypatch):
    """A code reload starts a new worker at once; the old one only finishes its own runs."""
    import asyncio
    import os

    from swarmforge.execution import Execution, ensure_allowance

    store = Store(cfg.db_path)
    release = asyncio.Event()
    spawned = []

    async def slow_run_item(c, st, item, **kw):
        st.create_run("long-run", budget_usd=5, max_runtime_s=600, pid=os.getpid())
        eid = ensure_allowance(st, "long-run", c)
        Execution(st, "long-run", eid).acquire(None)
        st.queue_update(item["id"], run_id="long-run")
        st.kv_set(runqueue.RESTART_KEY, "1")  # the owner resumed a project after new code landed
        await release.wait()
        st.queue_update(item["id"], status="done")
        return st.queue_item(item["id"])

    def fake_start_worker(c, st, keep_pause=False):
        spawned.append(st.kv_get(runqueue.PREVIOUS_WORKER_KEY))
        release.set()  # the old worker keeps finishing its run after handing over
        return 4242

    monkeypatch.setattr(runqueue, "run_item", slow_run_item)
    monkeypatch.setattr(runqueue, "start_worker", fake_start_worker)
    item = store.queue_add("Long", "x", "repoA")
    await asyncio.wait_for(runqueue.work(cfg, store, echo=None, poll_s=0.01), 10)
    assert spawned == [str(os.getpid())]  # handed over while the run was still going
    assert store.queue_item(item)["status"] == "done"  # and the old worker still finished it
    assert not store.kv_get(runqueue.RESTART_KEY)
    store.close()


async def test_new_worker_leaves_runs_of_the_worker_that_handed_over_alone(cfg, monkeypatch):
    import os
    import subprocess
    import sys

    store = Store(cfg.db_path)
    started = []

    async def fake_run_item(c, st, item, **kw):
        started.append(item["id"])
        st.queue_update(item["id"], status="done")
        return st.queue_item(item["id"])

    monkeypatch.setattr(runqueue, "run_item", fake_run_item)
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # noqa: ASYNC220 - a stand-in for the old worker
    try:
        busy = store.queue_add("Still running in the old worker", "x", "repoA", queue="a")
        store.queue_update(busy, status="running", run_id="old-run")
        free = store.queue_add("Other lane", "x", "repoB", queue="b")
        store.kv_set(runqueue.PREVIOUS_WORKER_KEY, str(old.pid))
        assert await runqueue.work(cfg, store, echo=None, poll_s=0.01, max_items=1) == 1
        assert started == [free]
        assert store.queue_item(busy)["status"] == "running"  # not requeued, not failed
    finally:
        old.kill()
        store.close()
    assert os.getpid()


async def test_idle_worker_waits_for_a_project_the_supervisor_will_reopen(cfg, monkeypatch):
    import asyncio

    from swarmforge.projects import autonomy

    store = Store(cfg.db_path)
    answers = [True, True, False]
    monkeypatch.setattr(autonomy, "supervision_pending", lambda *a: answers.pop(0))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(runqueue.asyncio, "sleep", lambda s: real_sleep(0))
    assert await asyncio.wait_for(runqueue.work(cfg, store, echo=None, poll_s=0.01), 5) == 0
    assert answers == []  # it kept polling until nothing could reopen, then exited as idle
    store.close()
