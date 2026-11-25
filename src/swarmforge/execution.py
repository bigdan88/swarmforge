"""Durable task allowances, fenced attempts and evidence-based recovery.

Only application code records progress. Repository/model text is evidence, never policy.
The total deadline is wall-clock time, including waits and downtime after first dispatch.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager

from .llm import BudgetExceeded


SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_allowances (
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, deadline REAL NOT NULL,
    budget_usd REAL NOT NULL, spent_usd REAL NOT NULL DEFAULT 0,
    active_run TEXT, owner_token TEXT, owner_pid INTEGER, heartbeat REAL,
    cancelled INTEGER NOT NULL DEFAULT 0, recoveries INTEGER NOT NULL DEFAULT 0,
    max_recoveries INTEGER NOT NULL, no_progress_s REAL NOT NULL, repeat_limit INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_attempts (
    run_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, parent_run TEXT,
    number INTEGER NOT NULL, started_at REAL NOT NULL, ended_at REAL,
    last_progress_at REAL NOT NULL, last_result TEXT, current_action TEXT,
    next_action TEXT, stop_kind TEXT, stop_reason TEXT,
    progress_wait_started REAL, progress_wait_s REAL NOT NULL DEFAULT 0,
    UNIQUE(execution_id, number)
);
CREATE INDEX IF NOT EXISTS execution_attempt_lineage ON execution_attempts(execution_id);
CREATE TABLE IF NOT EXISTS execution_evidence (
    execution_id TEXT NOT NULL, fingerprint TEXT NOT NULL, kind TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1, ts REAL NOT NULL, detail TEXT NOT NULL,
    PRIMARY KEY(execution_id, fingerprint, kind)
);
CREATE TABLE IF NOT EXISTS execution_calls (
    id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, run_id TEXT NOT NULL,
    reserved_usd REAL NOT NULL, cost_usd REAL, status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_call_lineage ON execution_calls(execution_id, status);
CREATE TABLE IF NOT EXISTS execution_checkpoints (
    execution_id TEXT NOT NULL, name TEXT NOT NULL, run_id TEXT NOT NULL,
    ts REAL NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
    PRIMARY KEY(execution_id, name)
);
"""


class ExecutionStopped(RuntimeError):
    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind


@contextmanager
def transaction(store):
    """Kept for existing callers: the Store's own transaction (one boundary, nested calls join it)."""
    if hasattr(store, "transaction"):
        with store.transaction() as conn:
            yield conn
        return
    with store._lock, store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        yield store.conn


def ensure_allowance(store, run_id, cfg, parent_run=None, execution_id=None, _seen=None):
    """Attach legacy ancestors lazily, retaining their known cost and original start time."""
    run = store.get_run(run_id)
    if run.get("execution_id"):
        return run["execution_id"]
    seen = set(_seen or ())
    if run_id in seen:
        raise ValueError("Task lineage contains a cycle; cannot allocate a new allowance")
    seen.add(run_id)
    parent = store.get_run(parent_run) if parent_run else None
    if parent_run and not parent:
        raise ValueError("Previous task not found; cannot reset its execution allowance")
    if parent:
        execution_id = ensure_allowance(store, parent["id"], cfg,
                                        parent.get("heal_of") or parent.get("retry_of") or parent.get("seed_run_id"), _seen=seen)
    with transaction(store) as db:
        existing = db.execute("SELECT execution_id FROM runs WHERE id=?", (run_id,)).fetchone()
        if existing and existing["execution_id"]:
            return existing["execution_id"]
        if not execution_id:
            execution_id = run_id
            budget = float(run.get("budget_usd") or cfg.limits.budget_usd)
            legacy_cap = cfg.self_heal.max_total_budget_usd
            if legacy_cap > 0:
                budget = min(budget, legacy_cap)
            db.execute("INSERT OR IGNORE INTO execution_allowances"
                       "(id,created_at,deadline,budget_usd,spent_usd,max_recoveries,no_progress_s,repeat_limit) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (execution_id, run["created_at"], run["created_at"] + float(run.get("max_runtime_s") or cfg.limits.max_runtime_s),
                        budget, store.cost_summary(run_id)["total"], cfg.limits.max_recoveries,
                        cfg.limits.no_progress_s, cfg.limits.repeat_action_limit))
        elif not db.execute("SELECT id FROM execution_allowances WHERE id=?", (execution_id,)).fetchone():
            raise ValueError("Unknown execution allowance")
        if parent and parent["id"] != run_id:
            db.execute("UPDATE execution_allowances SET spent_usd=spent_usd+? WHERE id=?",
                       (store.cost_summary(run_id)["total"], execution_id))
        # A project can impose a smaller remaining cap after lead/planning costs.
        # A retry configuration can reduce, but cannot enlarge, the root allowance.
        db.execute("UPDATE execution_allowances SET budget_usd=MIN(budget_usd,spent_usd+?) WHERE id=?",
                   (float(run.get("budget_usd") or cfg.limits.budget_usd), execution_id))
        db.execute("UPDATE runs SET execution_id=? WHERE id=?", (execution_id, run_id))
    return execution_id


class Execution:
    def __init__(self, store, run_id, execution_id):
        self.store, self.run_id, self.id = store, run_id, execution_id
        self.token = uuid.uuid4().hex

    def acquire(self, parent_run=None):
        from .launcher import pid_alive
        now = time.time()
        with transaction(self.store) as db:
            row = dict(db.execute("SELECT * FROM execution_allowances WHERE id=?", (self.id,)).fetchone())
            if row["active_run"]:
                # Never steal from a live process, even if its heartbeat is old.
                if pid_alive(row["owner_pid"]):
                    raise ExecutionStopped("concurrent_attempt", "Another attempt owns this task's remaining allowance")
                raise ExecutionStopped("lost_worker", "Recover the lost worker before starting another attempt")
            if row["cancelled"]:
                raise ExecutionStopped("cancelled", "Task lineage was intentionally cancelled")
            pending = db.execute("SELECT name FROM execution_checkpoints WHERE execution_id=? AND name LIKE 'external:%' "
                                 "AND status IN ('pending','uncertain','conflict') LIMIT 1", (self.id,)).fetchone()
            if pending:
                raise ExecutionStopped("external_uncertain", "Confirm the remote outcome before another attempt: " + pending["name"])
            previous = db.execute("SELECT stop_kind FROM execution_attempts WHERE execution_id=? ORDER BY number DESC LIMIT 1", (self.id,)).fetchone()
            if previous and previous["stop_kind"] == "stalled":
                if row["recoveries"] >= row["max_recoveries"]:
                    raise ExecutionStopped("stalled", "Bounded recovery used up; inspect retained evidence and revise the task")
                db.execute("UPDATE execution_allowances SET recoveries=recoveries+1 WHERE id=?", (self.id,))
            if now >= row["deadline"] or row["spent_usd"] >= row["budget_usd"]:
                raise ExecutionStopped("allowance_exhausted", "Shared task time or spending allowance exhausted")
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM execution_attempts WHERE execution_id=?", (self.id,)).fetchone()[0]
            db.execute("INSERT INTO execution_attempts(run_id,execution_id,parent_run,number,started_at,last_progress_at,next_action) "
                       "VALUES (?,?,?,?,?,?,?)", (self.run_id, self.id, parent_run, number, now, now, "Inspect retained work and execute the approved task"))
            # Repeated-action counting is per attempt: a fresh attempt naturally re-reads
            # the same files, and earlier attempts' counts must not stop it on its first step.
            # Repeated *failures* stay lineage-wide; they are real evidence of a loop.
            db.execute("DELETE FROM execution_evidence WHERE execution_id=? AND kind='action'", (self.id,))
            db.execute("UPDATE execution_allowances SET active_run=?,owner_token=?,owner_pid=?,heartbeat=? WHERE id=?",
                       (self.run_id, self.token, os.getpid(), now, self.id))

    def row(self):
        return self.store._one("SELECT * FROM execution_allowances WHERE id=?", (self.id,))

    def check(self):
        row = self.row()
        if row["owner_token"] != self.token:
            raise ExecutionStopped("lost_worker", "Attempt no longer owns the task allowance")
        if row["cancelled"] or self.store.cancel_requested(self.run_id):
            raise ExecutionStopped("cancelled", "Cancellation requested for this task")
        if time.time() >= row["deadline"]:
            raise ExecutionStopped("allowance_exhausted", "Shared task wall-clock time allowance exhausted")
        if row["spent_usd"] >= row["budget_usd"]:
            raise BudgetExceeded("Shared task spending allowance exhausted")

    def poll(self):
        self.check()
        self.store._exec("UPDATE execution_allowances SET heartbeat=? WHERE id=? AND owner_token=?", (time.time(), self.id, self.token))
        attempt = self.store._one("SELECT * FROM execution_attempts WHERE run_id=?", (self.run_id,))
        # Human approval waits have their own bounded timeout and do not count as stalls.
        waiting = self.store._one("SELECT id FROM approvals WHERE run_id=? AND status='pending' LIMIT 1", (self.run_id,))
        if waiting:
            self.store._exec("UPDATE execution_attempts SET progress_wait_started=COALESCE(progress_wait_started,?) WHERE run_id=?", (time.time(), self.run_id))
        elif attempt["progress_wait_started"]:
            self.store._exec("UPDATE execution_attempts SET progress_wait_s=progress_wait_s+?,progress_wait_started=NULL WHERE run_id=?", (time.time() - attempt["progress_wait_started"], self.run_id))
            attempt = self.store._one("SELECT * FROM execution_attempts WHERE run_id=?", (self.run_id,))
        if not waiting and time.time() - attempt["last_progress_at"] - attempt["progress_wait_s"] >= self.row()["no_progress_s"]:
            raise ExecutionStopped("stalled", "No new code, verification or investigation evidence within the progress window")
        repeated = self.store._one("SELECT kind,detail,occurrences FROM execution_evidence WHERE execution_id=? "
                                   "AND kind IN ('action','failure') AND occurrences>=? LIMIT 1",
                                   (self.id, self.row()["repeat_limit"]))
        if repeated:
            raise ExecutionStopped("stalled", f"Recurring {repeated['kind']} without useful progress: {repeated['detail'][:200]}")

    def observe(self, kind, detail, *, meaningful=False, next_action=None, summary=None):
        """Evidence is deduplicated across attempts. Repeating output never refreshes progress."""
        encoded = json.dumps(detail, sort_keys=True, default=str)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        now = time.time()
        with transaction(self.store) as db:
            known = db.execute("SELECT 1 FROM execution_evidence WHERE execution_id=? AND fingerprint=? AND kind=?", (self.id, fingerprint, kind)).fetchone()
            db.execute("INSERT INTO execution_evidence(execution_id,fingerprint,kind,ts,detail) VALUES (?,?,?,?,?) "
                       "ON CONFLICT(execution_id,fingerprint,kind) DO UPDATE SET occurrences=occurrences+1,ts=excluded.ts",
                       (self.id, fingerprint, kind, now, encoded[:4000]))
            if meaningful and not known:
                db.execute("UPDATE execution_attempts SET last_progress_at=?,last_result=?,next_action=?,progress_wait_s=0,progress_wait_started=NULL WHERE run_id=?",
                           (now, (summary or encoded)[:1000], next_action, self.run_id))
                db.execute("DELETE FROM execution_evidence WHERE execution_id=? AND kind='action'", (self.id,))
            if kind == "action":
                db.execute("UPDATE execution_attempts SET current_action=? WHERE run_id=?", ((summary or encoded)[:500], self.run_id))

    def checkpoint(self, name, detail, status="completed"):
        self.store._exec("INSERT INTO execution_checkpoints(execution_id,name,run_id,ts,status,detail) VALUES (?,?,?,?,?,?) "
                         "ON CONFLICT(execution_id,name) DO UPDATE SET run_id=excluded.run_id,ts=excluded.ts,status=excluded.status,detail=excluded.detail",
                         (self.id, name, self.run_id, time.time(), status, json.dumps(detail, default=str)))

    def finish(self, kind, reason):
        with transaction(self.store) as db:
            db.execute("UPDATE execution_attempts SET ended_at=?,stop_kind=?,stop_reason=? WHERE run_id=?", (time.time(), kind, reason, self.run_id))
            if kind == "cancelled":
                db.execute("UPDATE execution_allowances SET cancelled=1 WHERE id=? AND owner_token=?", (self.id, self.token))
            db.execute("UPDATE execution_allowances SET active_run=NULL,owner_token=NULL,owner_pid=NULL WHERE id=? AND owner_token=?", (self.id, self.token))
            db.execute("UPDATE runs SET stop_kind=? WHERE id=?", (kind, self.run_id))


class SharedBudget:
    """Budget facade plus durable, atomic in-flight reservations within the exclusive attempt."""
    def __init__(self, execution):
        self.execution = execution
        self.unpriced_calls = 0

    @property
    def limit(self):
        return self.execution.row()["budget_usd"]

    @property
    def spent(self):
        return self.execution.row()["spent_usd"]

    @property
    def remaining(self):
        return max(0.0, self.limit - self.spent)

    def check(self):
        self.execution.check()

    def add(self, usage):
        # Settlement is performed once, with the reservation, by LLMClient.
        pass

    def reserve(self, amount=None):
        self.check()
        e = self.execution
        with transaction(e.store) as db:
            row = dict(db.execute("SELECT * FROM execution_allowances WHERE id=?", (e.id,)).fetchone())
            if row["owner_token"] != e.token or row["cancelled"]:
                raise ExecutionStopped("cancelled", "Task reservation lost or cancelled")
            held = db.execute("SELECT COALESCE(SUM(reserved_usd),0) FROM execution_calls WHERE execution_id=? AND status='reserved'", (e.id,)).fetchone()[0]
            available = max(0.0, row["budget_usd"] - row["spent_usd"] - held)
            requested = amount if amount is not None else row["budget_usd"] - row["spent_usd"]
            if requested > available + 1e-9:
                if held:
                    return None
                raise BudgetExceeded("Remaining task allowance cannot cover the next model request")
            call_id = uuid.uuid4().hex
            db.execute("INSERT INTO execution_calls VALUES (?,?,?,?,NULL,'reserved')", (call_id, e.id, e.run_id, max(0.0, requested)))
            return call_id

    def settle(self, call_id, cost=None, *, uncertain=False, record=None):
        import math
        if cost is not None and (not math.isfinite(cost) or cost < 0):
            cost = None
        e = self.execution
        with transaction(e.store) as db:
            row = db.execute("SELECT * FROM execution_calls WHERE id=? AND status='reserved'", (call_id,)).fetchone()
            if row is None:
                return
            charged = row["reserved_usd"] if cost is None else cost
            db.execute("UPDATE execution_calls SET cost_usd=?,status=? WHERE id=?", (charged, "uncertain" if uncertain else "settled", call_id))
            db.execute("UPDATE execution_allowances SET spent_usd=spent_usd+? WHERE id=?", (charged, e.id))
            if record:
                cols = ",".join(["run_id", *record])
                placeholders = ",".join("?" for _ in range(len(record) + 1))
                db.execute(f"INSERT INTO llm_calls({cols}) VALUES ({placeholders})", (e.run_id, *record.values()))
            if uncertain:
                db.execute("INSERT INTO llm_calls(run_id,role,provider,model,started_at,ended_at,cost_usd,cost_source,status,error) "
                           "VALUES (?,'reservation','unknown','in-flight',?,?,?,'estimated','uncertain',?)",
                           (e.run_id, time.time(), time.time(), charged, "Request outcome unknown; retained reservation"))


def refresh_changed_files(ctx):
    """Observe shell/native-harness edits without accepting a model's change claims."""
    import subprocess
    try:
        result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                cwd=str(ctx.workspace), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return
    for line in result.stdout.splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path and not path.startswith(".swarmforge/"):
            ctx.files_written.add(path)


def observe_tool(execution, ctx, data, agent=None):
    """Classify completed tool evidence, never a heartbeat or free-form model claim.

    `agent` scopes repeated-action counting to one agent: several agents reading the
    same file is normal teamwork, not one agent looping."""
    name, args = data.get("name", ""), data.get("args", {})
    name = str(name).removeprefix("sf ")
    name = {"read": "read_file", "write": "write_file", "edit": "edit_file"}.get(name, name)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    output = str(data.get("output", ""))
    digest = hashlib.sha256(output.encode()).hexdigest()
    evidence = {"tool": name, "arguments": args, "result_hash": digest}
    action = name.replace("_", " ") + ": " + str(args.get("path") or args.get("command") or args.get("query") or "repository evidence")[:220]
    execution.observe("action", {"tool": name, "arguments": args, **({"agent": agent} if agent else {})}, summary=action)
    failed = output.startswith(("ERROR:", "REJECTED:")) or bool(data.get("failed"))
    command = (getattr(ctx, "commands_run", None) or [{}])[-1]
    if name in {"run_command", "run_tests", "check_code"} and (command.get("exit_code") or command.get("timed_out")):
        failed = True
    if failed:
        execution.observe("failure", {"tool": name, "arguments": args,
                                      "exit_code": command.get("exit_code"), "error": output[:300] if output.startswith("ERROR:") else None})
    # Reading different real repository evidence is useful investigation. Arbitrary
    # shell output and finish() claims cannot keep a stuck run alive.
    if not failed and name in {"read_file", "list_files", "search", "outline", "find_symbol", "git_changes", "git_history"}:
        execution.observe("investigation", evidence, meaningful=True, next_action="Apply findings to the approved task", summary=action)
    files = {}
    for file in ctx.files_written:
        path = (ctx.workspace / file).resolve()
        if not path.is_relative_to(ctx.workspace.resolve()):
            continue
        files[str(file)] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "deleted"
    if files:
        execution.observe("code", files, meaningful=True, next_action="Verify the changes",
                          summary=f"Updated {len(files)} file(s): " + ", ".join(files)[:250])


def snapshot(store, run_id):
    run = store.get_run(run_id)
    if not run or not run.get("execution_id"):
        return None
    row = store._one("SELECT * FROM execution_allowances WHERE id=?", (run["execution_id"],))
    attempt = store._one("SELECT * FROM execution_attempts WHERE run_id=?", (run_id,)) or {}
    if not row:
        return None
    latest = store._one("SELECT ended_at,stop_kind FROM execution_attempts WHERE execution_id=? ORDER BY number DESC LIMIT 1", (row["id"],)) or {}
    end = latest.get("ended_at") if latest.get("stop_kind") == "completed" else None
    held = store._one("SELECT COALESCE(SUM(reserved_usd),0) AS total FROM execution_calls WHERE execution_id=? AND status='reserved'", (row["id"],))["total"]
    pending = store._all("SELECT name,run_id,ts FROM execution_checkpoints WHERE execution_id=? AND name LIKE 'external:%' AND status='pending' LIMIT 10", (row["id"],))
    return {**attempt, "execution_id": row["id"], "created_at": row["created_at"], "deadline": row["deadline"],
            "stop_kind": attempt.get("stop_kind") or run.get("stop_kind"),
            "stop_reason": attempt.get("stop_reason") or run.get("state_reason"),
            "remaining_s": max(0, row["deadline"] - time.time()),
            "elapsed_s": max(0, (end or time.time()) - row["created_at"]),
            "budget_usd": row["budget_usd"], "spent_usd": row["spent_usd"],
            "reserved_usd": held, "available_usd": max(0, row["budget_usd"] - row["spent_usd"] - held),
            "pending_operations": pending,
            "remaining_usd": max(0, row["budget_usd"] - row["spent_usd"]),
            "recoveries": row["recoveries"], "max_recoveries": row["max_recoveries"]}


def progress_text(store, run_id):
    state = snapshot(store, run_id)
    if not state:
        return None
    last = state.get("last_progress_at")
    age = max(0, int(time.time() - last)) if last else None
    parts = [f"Attempt {state.get('number', 1)} · task elapsed {int(state['elapsed_s']) // 60} min · "
             f"{int(state['remaining_s']) // 60} min and ${state['remaining_usd']:.2f} remaining across all attempts."]
    if state["reserved_usd"]:
        parts.append(f"${state['reserved_usd']:.2f} is reserved for in-flight requests; ${state['available_usd']:.2f} is available for another request.")
    if state["pending_operations"]:
        parts.append("Confirm the remote outcome before retrying: " + ", ".join(p["name"] for p in state["pending_operations"]))
    if state.get("current_action"):
        parts.append("Current action: " + state["current_action"][:300])
    if state.get("last_result"):
        parts.append(f"Last meaningful result ({age}s ago): {state['last_result'][:300]}")
    else:
        parts.append("No meaningful result recorded yet.")
    if state.get("next_action"):
        parts.append("Next: " + state["next_action"])
    if state.get("stop_kind"):
        parts.append(f"Stopped: {state['stop_kind']}. {state.get('stop_reason') or ''}")
    return "\n\n".join(parts)


async def external_once(store, run_id, name, action, *, enforce_allowance=True, intent=None, cfg=None):
    """Record intent before an external write; uncertain outcomes require reconciliation.

    The operation's scope key includes the branch/PR, so an authorized replacement can
    publish its own work. A completed operation returns its recorded result on replay.
    `intent` (for example the commit being pushed) is kept with the record so reconciliation can
    tell this operation's effect from someone else's. If the call fails, the record becomes
    `uncertain`: the failure may have happened after GitHub acted. With `cfg`, an open record left by
    an earlier attempt is reconciled against GitHub first (see reconcile.py) instead of stopping.
    """
    run = store.get_run(run_id) or {}
    eid = run.get("execution_id") or run_id
    name = "external:" + name
    for reconciled in (False, True):
        with transaction(store) as db:
            existing = db.execute("SELECT * FROM execution_checkpoints WHERE execution_id=? AND name=?", (eid, name)).fetchone()
            if existing and existing["status"] == "completed":
                return json.loads(existing["detail"])
            if existing and existing["status"] == "conflict":
                raise ExecutionStopped("external_conflict", "External action needs you (reconciliation found a conflict): " + name)
            if not existing or existing["status"] == "not_done":
                allowance = db.execute("SELECT * FROM execution_allowances WHERE id=?", (eid,)).fetchone()
                if allowance:
                    if allowance["cancelled"]:
                        raise ExecutionStopped("cancelled", "Task cancelled before external action")
                    if enforce_allowance and (time.time() >= allowance["deadline"] or allowance["spent_usd"] >= allowance["budget_usd"]):
                        raise ExecutionStopped("allowance_exhausted", "Task allowance exhausted before external action")
                db.execute("INSERT INTO execution_checkpoints VALUES (?,?,?,?,?,?) ON CONFLICT(execution_id,name) DO UPDATE SET "
                           "run_id=excluded.run_id,ts=excluded.ts,status=excluded.status,detail=excluded.detail",
                           (eid, name, run_id, time.time(), "pending", json.dumps({"intent": intent or {}}, default=str)))
                break
        if reconciled or cfg is None:
            raise ExecutionStopped("external_uncertain", "External action outcome needs reconciliation before retry: " + name)
        # Open record from an earlier attempt: ask GitHub (outside any transaction), then decide again.
        from .github import GitHubClient
        from .reconcile import check_external, settle_checkpoint
        detail = json.loads(existing["detail"] or "null")
        verdict = await asyncio.to_thread(check_external, GitHubClient(cfg.github), name,
                                          (detail or {}).get("intent") if isinstance(detail, dict) else None)
        settle_checkpoint(store, eid, name, verdict)
    try:
        result = await action()
    except BaseException as exc:
        # The call may have failed after GitHub acted (timeout, dropped connection): never assume either way.
        store._exec("UPDATE execution_checkpoints SET status='uncertain',detail=?,ts=? WHERE execution_id=? AND name=? AND status='pending'",
                    (json.dumps({"intent": intent or {}, "error": f"{type(exc).__name__}: {exc}"[:500]}, default=str), time.time(), eid, name))
        raise
    store._exec("UPDATE execution_checkpoints SET status='completed',detail=?,ts=? WHERE execution_id=? AND name=?",
                (json.dumps(result, default=str), time.time(), eid, name))
    return result


def revise_allowance(store, run_id, budget_usd, runtime_s):
    """Explicit human settings change; configuration reloads never grant more allowance."""
    import math
    if not all(math.isfinite(x) and x > 0 for x in (budget_usd, runtime_s)):
        raise ValueError("Task allowance must be finite and positive")
    state = snapshot(store, run_id)
    if not state:
        return
    item = store._one("SELECT project_id,milestone FROM queue_items WHERE run_id=? AND project_id IS NOT NULL", (run_id,))
    if item:
        from .projects.store import Projects
        ps = Projects(store)
        project = ps.get(item["project_id"])
        remaining = project["budget_usd"] - ps.spent(project["id"])
        if budget_usd - state["spent_usd"] > remaining + 1e-9:
            raise ValueError("Task allowance cannot exceed the approved project budget")
        if budget_usd > state["budget_usd"]:
            raise ValueError("Project task increases require the existing project budget checkpoint")
    store._exec("UPDATE execution_allowances SET budget_usd=?,deadline=created_at+? WHERE id=?",
                (budget_usd, runtime_s, state["execution_id"]))


def approve_retry(store, cfg, run_id, *, budget=None, runtime_s=None):
    """Called only by existing human retry controls, never by automatic recovery."""
    run = store.get_run(run_id)
    eid = ensure_allowance(store, run_id, cfg, run.get("heal_of") or run.get("retry_of") or run.get("seed_run_id"))
    row = store._one("SELECT * FROM execution_allowances WHERE id=?", (eid,))
    revise_allowance(store, run_id, budget if budget is not None else row["budget_usd"],
                     runtime_s if runtime_s is not None else row["deadline"] - row["created_at"])
    store._exec("UPDATE execution_allowances SET cancelled=0 WHERE id=?", (eid,))
    store.record_intervention(run_id, "retry_approval", "Human approved retry within the shared task allowance; "
                              f"total budget={budget}, total wall-clock seconds={runtime_s}")


def recover(store, run_id):
    """Fence a dead attempt; never repeat an uncertain external operation."""
    from .launcher import pid_alive
    run = store.get_run(run_id)
    if not run or not run.get("execution_id"):
        return False
    eid = run["execution_id"]
    with transaction(store) as db:
        row = dict(db.execute("SELECT * FROM execution_allowances WHERE id=?", (eid,)).fetchone())
        prior = db.execute("SELECT ended_at FROM execution_attempts WHERE run_id=?", (run_id,)).fetchone()
        if prior and prior["ended_at"]:
            return False
        if row["active_run"] and pid_alive(row["owner_pid"]):
            return False
        # Unknown provider cost remains charged conservatively across restarts.
        reservations = db.execute("SELECT run_id,SUM(reserved_usd) AS held FROM execution_calls WHERE execution_id=? AND status='reserved' GROUP BY run_id", (eid,)).fetchall()
        held = sum(r["held"] for r in reservations)
        for reservation in reservations:
            db.execute("INSERT INTO llm_calls(run_id,role,provider,model,started_at,ended_at,cost_usd,cost_source,status,error) "
                       "VALUES (?,'reservation','unknown','in-flight',?,?,?,'estimated','uncertain','Lost worker; retained reservation')",
                       (reservation["run_id"], time.time(), time.time(), reservation["held"]))
        db.execute("UPDATE execution_calls SET cost_usd=reserved_usd,status='uncertain' WHERE execution_id=? AND status='reserved'", (eid,))
        db.execute("UPDATE execution_allowances SET spent_usd=spent_usd+?,active_run=NULL,owner_token=NULL,owner_pid=NULL WHERE id=?", (held, eid))
        external = db.execute("SELECT name FROM execution_checkpoints WHERE execution_id=? AND name LIKE 'external:%' LIMIT 1", (eid,)).fetchone()
        if run["state"] in {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}:
            pending = db.execute("SELECT name FROM execution_checkpoints WHERE execution_id=? AND name LIKE 'external:%' AND status='pending' LIMIT 1", (eid,)).fetchone()
            kind = "external_uncertain" if pending else run.get("stop_kind") or ("completed" if run["state"] == "COMPLETED" else "task_failure")
            reason = "Completed run retained; reconcile pending external action before retry" if pending else run.get("state_reason") or "Recovered terminal run without replay"
            db.execute("UPDATE execution_attempts SET ended_at=?,stop_kind=?,stop_reason=? WHERE run_id=?", (time.time(), kind, reason, run_id))
            db.execute("UPDATE runs SET stop_kind=? WHERE id=?", (kind, run_id))
            return False
        ok = not external and not row["cancelled"] and row["recoveries"] < row["max_recoveries"] and time.time() < row["deadline"] and row["spent_usd"] + held < row["budget_usd"]
        reason = "Lost worker; recover from retained code" if ok else "Lost worker; allowance exhausted, recovery cap reached, cancelled, or external action requires reconciliation"
        db.execute("UPDATE execution_attempts SET ended_at=?,stop_kind='lost_worker',stop_reason=? WHERE run_id=?", (time.time(), reason, run_id))
        db.execute("UPDATE runs SET state='FAILED',stop_kind='lost_worker',state_reason=?,finished_at=? WHERE id=?", (reason, time.time(), run_id))
        if ok:
            db.execute("UPDATE execution_allowances SET recoveries=recoveries+1 WHERE id=?", (eid,))
    store.event(run_id, "recovery", reason, level="warn")
    return bool(ok)
