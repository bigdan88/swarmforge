"""SQLite persistence for run state and telemetry.

The database is the single source of truth that the CLI `status`/`logs` commands and the
Streamlit dashboard read. Every write is committed immediately so interrupted runs remain
inspectable.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    mode TEXT NOT NULL DEFAULT 'swarm',
    issue_url TEXT,
    issue_title TEXT,
    issue_number INTEGER,
    repo TEXT,
    base_branch TEXT,
    integration_branch TEXT,
    run_dir TEXT,
    state TEXT NOT NULL,
    state_reason TEXT,
    repair_cycle INTEGER NOT NULL DEFAULT 0,
    budget_usd REAL,
    pr_url TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    plan_json TEXT,
    config_json TEXT,
    pid INTEGER
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    task_key TEXT NOT NULL,
    cycle INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'implement',
    title TEXT,
    description TEXT,
    role TEXT,
    model TEXT,
    depends_on TEXT,
    acceptance_criteria TEXT,
    expected_files TEXT,
    state TEXT NOT NULL,
    branch TEXT,
    worktree TEXT,
    started_at REAL,
    ended_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    error TEXT,
    UNIQUE(run_id, task_key)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    task_key TEXT,
    role TEXT,
    message TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    task_key TEXT,
    role TEXT,
    provider TEXT,
    model TEXT,
    started_at REAL,
    ended_at REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cached_tokens INTEGER,
    cost_usd REAL,
    cost_source TEXT,
    status TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    cycle INTEGER,
    label TEXT,
    command TEXT,
    exit_code INTEGER,
    passed INTEGER,
    failed INTEGER,
    errors INTEGER,
    skipped INTEGER,
    duration_s REAL,
    log_path TEXT,
    junit_path TEXT,
    timed_out INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    cycle INTEGER,
    source TEXT,
    severity TEXT,
    file TEXT,
    line INTEGER,
    title TEXT,
    description TEXT,
    recommendation TEXT
);
CREATE TABLE IF NOT EXISTS acceptance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    cycle INTEGER,
    criterion_id TEXT,
    text TEXT,
    status TEXT,
    evidence TEXT
);
CREATE TABLE IF NOT EXISTS interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_at REAL,
    decided_by TEXT
);
CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    label TEXT,
    issue TEXT,
    results_json TEXT
);
"""


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------------- helpers
    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------- runs
    def create_run(self, run_id: str, **fields: Any) -> None:
        now = time.time()
        fields = {"created_at": now, "updated_at": now, "state": "ISSUE_RECEIVED", **fields}
        cols = ", ".join(["id", *fields])
        qs = ", ".join(["?"] * (len(fields) + 1))
        self._exec(f"INSERT INTO runs ({cols}) VALUES ({qs})", (run_id, *fields.values()))

    def update_run(self, run_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))

    def get_run(self, run_id: str) -> dict | None:
        if run_id == "latest":
            return self._one("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1")
        row = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if row is None:  # allow unique prefix
            rows = self._all("SELECT * FROM runs WHERE id LIKE ?", (run_id + "%",))
            if len(rows) == 1:
                row = rows[0]
        return row

    def list_runs(self, limit: int = 20) -> list[dict]:
        return self._all("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))

    def request_cancel(self, run_id: str) -> None:
        self.update_run(run_id, cancel_requested=1)

    def cancel_requested(self, run_id: str) -> bool:
        row = self._one("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,))
        return bool(row and row["cancel_requested"])

    # ------------------------------------------------------------------ tasks
    def upsert_task(self, run_id: str, task_key: str, **fields: Any) -> None:
        for k in ("depends_on", "acceptance_criteria", "expected_files"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = _j(fields[k])
        existing = self._one(
            "SELECT id FROM tasks WHERE run_id = ? AND task_key = ?", (run_id, task_key)
        )
        if existing:
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                self._exec(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), existing["id"]))
        else:
            fields.setdefault("state", "PENDING")
            cols = ", ".join(["run_id", "task_key", *fields])
            qs = ", ".join(["?"] * (len(fields) + 2))
            self._exec(f"INSERT INTO tasks ({cols}) VALUES ({qs})", (run_id, task_key, *fields.values()))

    def tasks(self, run_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM tasks WHERE run_id = ? ORDER BY id", (run_id,))
        for r in rows:
            for k in ("depends_on", "acceptance_criteria", "expected_files"):
                r[k] = json.loads(r[k]) if r[k] else []
        return rows

    # ----------------------------------------------------------------- events
    def event(self, run_id: str, kind: str, message: str, *, level: str = "info",
              task_key: str | None = None, role: str | None = None, data: Any = None) -> None:
        self._exec(
            "INSERT INTO events (run_id, ts, level, kind, task_key, role, message, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, time.time(), level, kind, task_key, role, message, _j(data)),
        )

    def events(self, run_id: str, after_id: int = 0, limit: int = 10_000) -> list[dict]:
        return self._all(
            "SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
            (run_id, after_id, limit),
        )

    # -------------------------------------------------------------- llm calls
    def record_llm_call(self, run_id: str, **fields: Any) -> None:
        cols = ", ".join(["run_id", *fields])
        qs = ", ".join(["?"] * (len(fields) + 1))
        self._exec(f"INSERT INTO llm_calls ({cols}) VALUES ({qs})", (run_id, *fields.values()))

    def llm_calls(self, run_id: str) -> list[dict]:
        return self._all("SELECT * FROM llm_calls WHERE run_id = ? ORDER BY id", (run_id,))

    def cost_summary(self, run_id: str) -> dict:
        row = self._one(
            """SELECT
                 COALESCE(SUM(CASE WHEN cost_source='provider_reported' THEN cost_usd END),0) AS measured,
                 COALESCE(SUM(CASE WHEN cost_source='estimated' THEN cost_usd END),0) AS estimated,
                 SUM(CASE WHEN cost_source='unavailable' THEN 1 ELSE 0 END) AS unavailable_calls,
                 COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                 COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                 COALESCE(SUM(total_tokens),0) AS total_tokens,
                 SUM(CASE WHEN prompt_tokens IS NULL THEN 1 ELSE 0 END) AS calls_without_usage,
                 COUNT(*) AS calls
               FROM llm_calls WHERE run_id = ?""",
            (run_id,),
        ) or {}
        row["total"] = (row.get("measured") or 0) + (row.get("estimated") or 0)
        return row

    def cost_by_role(self, run_id: str) -> list[dict]:
        return self._all(
            """SELECT role, model, COUNT(*) AS calls,
                      COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                      COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                      COALESCE(SUM(cost_usd),0) AS cost_usd,
                      GROUP_CONCAT(DISTINCT cost_source) AS cost_sources
               FROM llm_calls WHERE run_id = ? GROUP BY role, model ORDER BY role""",
            (run_id,),
        )

    # ------------------------------------------------------------------ tests
    def record_test_run(self, run_id: str, **fields: Any) -> int:
        fields.setdefault("ts", time.time())
        cols = ", ".join(["run_id", *fields])
        qs = ", ".join(["?"] * (len(fields) + 1))
        return self._exec(f"INSERT INTO test_runs ({cols}) VALUES ({qs})",
                          (run_id, *fields.values())).lastrowid

    def test_runs(self, run_id: str) -> list[dict]:
        return self._all("SELECT * FROM test_runs WHERE run_id = ? ORDER BY id", (run_id,))

    # --------------------------------------------------------------- findings
    def record_finding(self, run_id: str, **fields: Any) -> None:
        cols = ", ".join(["run_id", *fields])
        qs = ", ".join(["?"] * (len(fields) + 1))
        self._exec(f"INSERT INTO findings ({cols}) VALUES ({qs})", (run_id, *fields.values()))

    def findings(self, run_id: str, cycle: int | None = None) -> list[dict]:
        if cycle is None:
            return self._all("SELECT * FROM findings WHERE run_id = ? ORDER BY id", (run_id,))
        return self._all("SELECT * FROM findings WHERE run_id = ? AND cycle = ? ORDER BY id",
                         (run_id, cycle))

    def record_acceptance(self, run_id: str, **fields: Any) -> None:
        cols = ", ".join(["run_id", *fields])
        qs = ", ".join(["?"] * (len(fields) + 1))
        self._exec(f"INSERT INTO acceptance ({cols}) VALUES ({qs})", (run_id, *fields.values()))

    def acceptance(self, run_id: str) -> list[dict]:
        return self._all("SELECT * FROM acceptance WHERE run_id = ? ORDER BY id", (run_id,))

    # ---------------------------------------------------------- interventions
    def record_intervention(self, run_id: str, kind: str, detail: str = "") -> None:
        self._exec("INSERT INTO interventions (run_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                   (run_id, time.time(), kind, detail))

    def interventions(self, run_id: str) -> list[dict]:
        return self._all("SELECT * FROM interventions WHERE run_id = ? ORDER BY id", (run_id,))

    # -------------------------------------------------------------- approvals
    def request_approval(self, run_id: str, question: str) -> int:
        return self._exec("INSERT INTO approvals (run_id, ts, question) VALUES (?, ?, ?)",
                          (run_id, time.time(), question)).lastrowid

    def decide_approval(self, approval_id: int, approved: bool, by: str = "dashboard") -> None:
        self._exec("UPDATE approvals SET status = ?, decided_at = ?, decided_by = ? WHERE id = ? AND status = 'pending'",
                   ("approved" if approved else "declined", time.time(), by, approval_id))

    def approval(self, approval_id: int) -> dict | None:
        return self._one("SELECT * FROM approvals WHERE id = ?", (approval_id,))

    def pending_approvals(self, run_id: str | None = None) -> list[dict]:
        if run_id:
            return self._all("SELECT * FROM approvals WHERE status = 'pending' AND run_id = ? ORDER BY id", (run_id,))
        return self._all("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id")

    # ------------------------------------------------------------- benchmarks
    def record_benchmark(self, label: str, issue: str, results: Any) -> int:
        return self._exec(
            "INSERT INTO benchmarks (created_at, label, issue, results_json) VALUES (?, ?, ?, ?)",
            (time.time(), label, issue, _j(results)),
        ).lastrowid

    def benchmarks(self) -> list[dict]:
        rows = self._all("SELECT * FROM benchmarks ORDER BY id DESC")
        for r in rows:
            r["results"] = json.loads(r.pop("results_json") or "null")
        return rows
