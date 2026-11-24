"""Durable job ownership: one active job per subject, leases, owner-only finish, explicit recovery."""
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from swarmforge import jobs
from swarmforge.db import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "jobs.db")
    yield s
    s.close()


def _other_owner(store, job_id, *, pid=2147480000, host=None, lease=60.0):
    """Pretend another process owns the job."""
    store._exec("UPDATE jobs SET owner='someone-else', owner_pid=?, owner_host=?, lease_until=? WHERE id=?",
                (pid, host or jobs.ME.host, time.time() + lease, job_id))


def test_one_active_job_per_subject_across_connections(store, tmp_path):
    job = jobs.create(store, "demo", "s1")
    assert job["state"] == "running" and job["owner"] == jobs.ME.instance and job["attempt"] == 1
    other = Store(tmp_path / "jobs.db")
    try:
        with pytest.raises(jobs.JobBusy):
            jobs.create(other, "demo", "s1")
        jobs.create(other, "demo", "s2")  # another subject is independent
        jobs.create(other, "other-kind", "s1")
    finally:
        other.close()
    assert jobs.finish(store, job["id"], "succeeded")
    retry = jobs.create(store, "demo", "s1", previous_id=job["id"])
    assert retry["attempt"] == 2 and retry["previous_id"] == job["id"]


def test_only_the_owner_finishes_and_terminal_states_never_change(store):
    job = jobs.create(store, "demo", "s")
    _other_owner(store, job["id"])
    assert not jobs.finish(store, job["id"], "succeeded")  # not ours
    assert jobs.get(store, job["id"])["state"] == "running"
    store._exec("UPDATE jobs SET owner=? WHERE id=?", (jobs.ME.instance, job["id"]))
    assert jobs.finish(store, job["id"], "failed", error="boom")
    assert not jobs.finish(store, job["id"], "succeeded")
    assert jobs.get(store, job["id"])["state"] == "failed"
    assert [h["to_state"] for h in jobs.history(store, job["id"])] == ["running", "failed"]
    with pytest.raises(ValueError):
        jobs.finish(store, job["id"], "running")


def test_pending_jobs_are_claimed_once(store):
    job = jobs.create(store, "demo", "s", run=False)
    assert job["state"] == "pending" and job["owner"] is None
    assert jobs.claim(store, job["id"])["owner"] == jobs.ME.instance
    assert jobs.claim(store, job["id"]) is None


@pytest.mark.parametrize("pid,lease,host,recovered", [
    (2147480000, 60, None, True),        # owner process on this host has exited
    ("me", 60, None, False),             # live owner with a valid lease
    ("me", -1, None, True),              # live process, but it stopped renewing its lease
    (2147480000, 60, "elsewhere", False),  # another host: only its lease can tell
])
def test_recovery_needs_proof_the_owner_is_gone(store, pid, lease, host, recovered):
    job = jobs.create(store, "demo", "s")
    _other_owner(store, job["id"], pid=os.getpid() if pid == "me" else pid, lease=lease, host=host)
    moved = jobs.recover(store)
    assert bool(moved) == recovered
    state = jobs.get(store, job["id"])["state"]
    assert state == ("interrupted" if recovered else "running")
    if recovered:
        assert jobs.history(store, job["id"])[-1]["reason"].startswith("recovered:")


def test_kinds_with_external_effects_recover_as_uncertain_and_hooks_run_in_the_same_transaction(store):
    seen = []
    jobs.register("publish-demo", recover_to="uncertain", on_transition=lambda conn, job, to, why: seen.append((job["subject"], to)))
    job = jobs.create(store, "publish-demo", "pr-7")
    _other_owner(store, job["id"], lease=-1)
    jobs.recover(store, "publish-demo")
    assert jobs.get(store, job["id"])["state"] == "uncertain" and seen == [("pr-7", "uncertain")]


def test_keep_alive_renews_and_reports_a_lost_lease(store):
    job = jobs.create(store, "demo", "s", lease_s=0.3)
    with jobs.keep_alive(store, job["id"], every=0.05, lease_s=0.3) as lost:
        time.sleep(0.5)  # longer than the lease: renewals keep it
        assert jobs.recover(store) == [] and not lost.is_set()
        _other_owner(store, job["id"])  # someone else took over
        assert lost.wait(1.0)


def test_transactions_are_all_or_nothing_and_helpers_join_them(store):
    store._exec("CREATE TABLE t (v TEXT)")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store._exec("INSERT INTO t VALUES ('a')")  # joins: no commit of its own
            with store.transaction():  # nested joins the outer one
                store._exec("INSERT INTO t VALUES ('b')")
            raise RuntimeError("boom")
    assert store._all("SELECT * FROM t") == []
    with store.transaction():
        store._exec("INSERT INTO t VALUES ('c')")
    assert store._all("SELECT v FROM t") == [{"v": "c"}]
    # a failing hook rolls the job back with it
    with pytest.raises(ZeroDivisionError):
        jobs.create(store, "demo", "atomic", also=lambda conn, job: 1 / 0)
    assert jobs.active(store, "demo", "atomic") is None


HOLDER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from swarmforge import jobs
    from swarmforge.db import Store
    store = Store(Path(sys.argv[1]))
    job = jobs.create(store, "demo", "shared", lease_s=float(sys.argv[2]))
    print(job["id"], flush=True)
    with jobs.keep_alive(store, job["id"], every=0.1, lease_s=float(sys.argv[2])):
        time.sleep(60)
""")


def _holder(tmp_path, lease="2"):
    script = tmp_path / "holder.py"
    script.write_text(HOLDER)
    proc = subprocess.Popen([sys.executable, str(script), str(tmp_path / "jobs.db"), lease], stdout=subprocess.PIPE, text=True)
    job_id = proc.stdout.readline().strip()
    assert job_id, "holder process did not start"
    return proc, job_id


def test_a_live_owner_in_another_process_is_never_disturbed(store, tmp_path):
    proc, job_id = _holder(tmp_path, lease="0.5")
    try:
        time.sleep(1.2)  # well past one lease: the owner keeps renewing
        assert jobs.recover(store) == []
        with pytest.raises(jobs.JobBusy):
            jobs.create(store, "demo", "shared")
        assert not jobs.finish(store, job_id, "succeeded")
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_a_killed_owner_is_recovered_at_once_and_the_subject_is_free_again(store, tmp_path):
    proc, job_id = _holder(tmp_path, lease="60")
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    moved = jobs.recover(store)
    assert [m["id"] for m in moved] == [job_id]
    assert "has exited" in jobs.history(store, job_id)[-1]["reason"]
    assert jobs.create(store, "demo", "shared")["state"] == "running"


def test_concurrent_creates_have_exactly_one_winner(tmp_path):
    winners, errors = [], []

    def attempt():
        s = Store(tmp_path / "jobs.db")
        try:
            winners.append(jobs.create(s, "demo", "race")["id"])
        except jobs.JobBusy:
            errors.append(1)
        finally:
            s.close()
    Store(tmp_path / "jobs.db").close()
    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1 and len(errors) == 7
