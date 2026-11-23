"""Durable background jobs: one persisted owner, a renewable lease and explicit state transitions.

Every piece of background work (a chat reply, a project-lead turn, a run, a queue worker) is a row
in `jobs`. Exactly one active job may exist per (kind, subject); the database enforces it across
connections and processes. The owner renews its lease while it works. Only the owner can finish
its job, and only while it still holds the lease. Any process may *recover* a job whose owner is
gone: its lease expired, or its process on this host no longer exists. Recovery is an explicit
transition (to `interrupted`, or to `uncertain` for work whose external effect may already have
happened) recorded with its reason in `job_transitions`; nothing is ever retried automatically.

States:   pending -> running -> succeeded | failed | interrupted | uncertain | cancelled
          pending -> cancelled
Terminal states never change. A retry is a new job (attempt + 1) that names its predecessor.

Hooks let the subsystem that owns a kind update its own rows in the *same* transaction as the job
transition (for example the chat request shown in the browser), so the two can never disagree.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .db import Store

ACTIVE = ("pending", "running")
TERMINAL = ("succeeded", "failed", "interrupted", "uncertain", "cancelled")
ALLOWED = {
    "pending": {"running", "cancelled"},
    "running": {"succeeded", "failed", "interrupted", "uncertain", "cancelled"},
}
DEFAULT_LEASE_S = 60.0
RENEW_EVERY_S = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, state TEXT NOT NULL,
    owner TEXT, owner_pid INTEGER, owner_host TEXT, lease_until REAL,
    attempt INTEGER NOT NULL DEFAULT 1, previous_id TEXT, detail TEXT NOT NULL DEFAULT '{}',
    error TEXT, created_at REAL NOT NULL, started_at REAL, finished_at REAL, renewed_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active ON jobs(kind, subject) WHERE state IN ('pending','running');
CREATE INDEX IF NOT EXISTS jobs_running ON jobs(state, lease_until);
CREATE TABLE IF NOT EXISTS job_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts REAL NOT NULL,
    from_state TEXT, to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS job_transitions_job ON job_transitions(job_id, id);
"""


class JobBusy(ValueError):
    """Another active job already exists for this kind and subject."""


class LeaseLost(RuntimeError):
    """This process no longer owns the job (it was recovered by someone else)."""


@dataclass(frozen=True)
class Owner:
    instance: str  # unique per process start; a restarted process is a new owner
    pid: int
    host: str

    def __str__(self) -> str:
        return self.instance


ME = Owner(f"{os.getpid()}:{uuid.uuid4().hex[:12]}", os.getpid(), socket.gethostname())

# kind -> fn(conn, job, to_state, reason): keeps the kind's own rows in step, inside the transition.
_HOOKS: dict[str, Callable[[Any, dict, str, str], None]] = {}
# kind -> state a recovered job takes; work that may have reached the outside world is "uncertain".
_RECOVER_TO: dict[str, str] = {}


def register(kind: str, *, on_transition: Callable[[Any, dict, str, str], None] | None = None,
             recover_to: str = "interrupted") -> None:
    if recover_to not in ("interrupted", "uncertain"):
        raise ValueError("A recovered job becomes interrupted or uncertain")
    if on_transition is not None:
        _HOOKS[kind] = on_transition
    _RECOVER_TO[kind] = recover_to


def ensure_tables(store: Store) -> None:
    if getattr(store, "_jobs_ready", False):
        return
    with store._lock:
        store.conn.executescript(SCHEMA)
        store.conn.commit()
    store._jobs_ready = True  # type: ignore[attr-defined]


def _row(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["detail"] = json.loads(job["detail"] or "{}")
    return job


def get(store: Store, job_id: str) -> dict | None:
    ensure_tables(store)
    with store._lock:
        return _row(store.conn, job_id)


def active(store: Store, kind: str, subject: str) -> dict | None:
    ensure_tables(store)
    with store._lock:
        row = store.conn.execute("SELECT id FROM jobs WHERE kind=? AND subject=? AND state IN ('pending','running')",
                                 (kind, subject)).fetchone()
        return _row(store.conn, row["id"]) if row else None


def history(store: Store, job_id: str) -> list[dict]:
    ensure_tables(store)
    return store._all("SELECT ts,from_state,to_state,actor,reason FROM job_transitions WHERE job_id=? ORDER BY id", (job_id,))


def _record(conn, job: dict, to_state: str, actor: str, reason: str) -> None:
    conn.execute("INSERT INTO job_transitions(job_id,ts,from_state,to_state,actor,reason) VALUES (?,?,?,?,?,?)",
                 (job["id"], time.time(), job["state"], to_state, actor, reason[:2000]))
    hook = _HOOKS.get(job["kind"])
    if hook is not None:
        hook(conn, job, to_state, reason)


def create(store: Store, kind: str, subject: str, *, detail: dict | None = None, run: bool = True,
           lease_s: float = DEFAULT_LEASE_S, previous_id: str | None = None,
           also: Callable[[Any, dict], None] | None = None) -> dict:
    """Create a job; with run=True this process owns it and it starts `running` at once.

    `also(conn, job)` writes the caller's own rows in the same transaction (all or nothing).
    Raises JobBusy if (kind, subject) already has an active job."""
    import sqlite3
    ensure_tables(store)
    now = time.time()
    job_id = uuid.uuid4().hex
    attempt = 1
    try:
        with store.transaction() as conn:
            if previous_id:
                prev = _row(conn, previous_id)
                attempt = (prev["attempt"] + 1) if prev else 1
            state = "running" if run else "pending"
            conn.execute("INSERT INTO jobs(id,kind,subject,state,owner,owner_pid,owner_host,lease_until,attempt,previous_id,"
                         "detail,created_at,started_at,renewed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (job_id, kind, subject, state, ME.instance if run else None, ME.pid if run else None,
                          ME.host if run else None, now + lease_s if run else None, attempt, previous_id,
                          json.dumps(detail or {}), now, now if run else None, now if run else None))
            job = _row(conn, job_id)
            assert job is not None
            conn.execute("INSERT INTO job_transitions(job_id,ts,from_state,to_state,actor,reason) VALUES (?,?,?,?,?,?)",
                         (job_id, now, None, state, ME.instance, "created"))
            if also is not None:
                also(conn, job)
    except sqlite3.IntegrityError as exc:
        if "jobs.kind" in str(exc):
            raise JobBusy(f"A {kind} job is already active for {subject}") from None
        raise
    return job


def claim(store: Store, job_id: str, *, lease_s: float = DEFAULT_LEASE_S) -> dict | None:
    """Take a pending job; returns it, or None if someone else got it first."""
    ensure_tables(store)
    now = time.time()
    with store.transaction() as conn:
        job = _row(conn, job_id)
        if not job or job["state"] != "pending":
            return None
        conn.execute("UPDATE jobs SET state='running',owner=?,owner_pid=?,owner_host=?,lease_until=?,started_at=?,renewed_at=? "
                     "WHERE id=? AND state='pending'", (ME.instance, ME.pid, ME.host, now + lease_s, now, now, job_id))
        _record(conn, job, "running", ME.instance, "claimed")
        return _row(conn, job_id)


def renew(store: Store, job_id: str, *, lease_s: float = DEFAULT_LEASE_S) -> bool:
    """Extend this process's lease. False means the job is no longer ours: stop working on it."""
    now = time.time()
    cur = store._exec("UPDATE jobs SET lease_until=?, renewed_at=? WHERE id=? AND state='running' AND owner=?",
                      (now + lease_s, now, job_id, ME.instance))
    return cur.rowcount == 1


def finish(store: Store, job_id: str, state: str, *, error: str | None = None, reason: str = "",
           also: Callable[[Any, dict], None] | None = None) -> bool:
    """End a job this process owns. Returns False (and changes nothing) if it was recovered meanwhile."""
    if state not in TERMINAL:
        raise ValueError(f"{state} is not a terminal job state")
    with store.transaction() as conn:
        job = _row(conn, job_id)
        if not job or job["state"] not in ALLOWED or state not in ALLOWED[job["state"]]:
            return False
        if job["state"] == "running" and job["owner"] != ME.instance:
            return False
        conn.execute("UPDATE jobs SET state=?,error=?,finished_at=?,lease_until=NULL WHERE id=? AND state=?",
                     (state, error, time.time(), job_id, job["state"]))
        _record(conn, job, state, ME.instance, reason or (error or state))
        if also is not None:
            also(conn, job)
    return True


def _owner_gone(job: dict, now: float) -> str | None:
    """Why the owner is gone, or None if it may still be working."""
    if job["owner"] == ME.instance:
        return None
    if job["lease_until"] is None or job["lease_until"] < now:
        return "its lease expired (the owner stopped renewing it)"
    if job["owner_host"] == ME.host and job["owner_pid"]:
        from .launcher import pid_alive
        if not pid_alive(job["owner_pid"]):
            return f"its owner process {job['owner_pid']} has exited"
    return None


def recover(store: Store, kind: str | None = None, subject: str | None = None) -> list[dict]:
    """Move running jobs whose owner is gone to their kind's recovery state. Returns the jobs moved."""
    ensure_tables(store)
    now = time.time()
    where, params = "state='running'", []
    if kind:
        where += " AND kind=?"
        params.append(kind)
    if subject:
        where += " AND subject=?"
        params.append(subject)
    moved = []
    for candidate in store._all(f"SELECT id FROM jobs WHERE {where}", tuple(params)):
        with store.transaction() as conn:
            job = _row(conn, candidate["id"])  # re-read under the write lock: no race with a renewal
            if not job or job["state"] != "running":
                continue
            why = _owner_gone(job, now)
            if why is None:
                continue
            to = _RECOVER_TO.get(job["kind"], "interrupted")
            conn.execute("UPDATE jobs SET state=?,error=?,finished_at=?,lease_until=NULL WHERE id=? AND state='running'",
                         (to, f"Recovered: {why}", now, job["id"]))
            _record(conn, job, to, ME.instance, f"recovered: {why}")
            moved.append({**job, "state": to})
    return moved


@contextmanager
def keep_alive(store: Store, job_id: str, *, every: float = RENEW_EVERY_S,
               lease_s: float = DEFAULT_LEASE_S) -> Iterator[threading.Event]:
    """Renew the lease in the background while the block runs. The yielded event is set if the
    lease is lost, so long work can check it and stop instead of finishing someone else's job."""
    lost, done = threading.Event(), threading.Event()

    def beat():
        while not done.wait(every):
            try:
                if not renew(store, job_id, lease_s=lease_s):
                    lost.set()
                    return
            except Exception:  # noqa: BLE001 - a transient database error must not kill the worker
                continue

    thread = threading.Thread(target=beat, name=f"job-lease-{job_id[:8]}", daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        done.set()
