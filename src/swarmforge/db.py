"""SQLite persistence for run state and telemetry.

The database is the single source of truth that the CLI `status`/`logs` commands and the
Streamlit dashboard read. Every write is committed immediately so interrupted runs remain
inspectable.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, repo TEXT NOT NULL, spec_path TEXT NOT NULL DEFAULT 'docs/project-spec.md',
    conversation_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'drafting', current_milestone INTEGER NOT NULL DEFAULT 0,
    budget_usd REAL NOT NULL, created_at REAL NOT NULL, spec TEXT NOT NULL, definition TEXT NOT NULL,
    observed_sha TEXT, last_error TEXT, approved_at REAL
);
CREATE TABLE IF NOT EXISTS milestones (
    project_id TEXT NOT NULL, idx INTEGER NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ahead',
    estimate_usd REAL NOT NULL, estimate_hours REAL NOT NULL, spent_usd REAL NOT NULL DEFAULT 0,
    started_at REAL, finished_at REAL, report TEXT, PRIMARY KEY(project_id, idx)
);
CREATE TABLE IF NOT EXISTS project_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
);
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
CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position REAL NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    repo TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'swarm',
    budget REAL,
    max_cycles INTEGER,
    chain INTEGER NOT NULL DEFAULT 1,
    approval TEXT NOT NULL DEFAULT 'yes',
    status TEXT NOT NULL DEFAULT 'queued',
    run_id TEXT,
    base_branch TEXT,
    result_branch TEXT,
    note TEXT,
    created_at REAL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS variants (
    id TEXT PRIMARY KEY,
    parent TEXT,
    created_at REAL NOT NULL,
    overlay TEXT NOT NULL,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    scores TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    title TEXT,
    repo TEXT,
    state TEXT,
    reason TEXT,
    category TEXT,
    root_cause TEXT,
    fix TEXT,
    lesson TEXT,
    retry INTEGER NOT NULL DEFAULT 0,
    diagnosed_by TEXT,
    evidence TEXT,
    note_stem TEXT,
    heal_run_id TEXT,
    outcome TEXT,
    cost_usd REAL,
    elapsed_s REAL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    label TEXT,
    issue TEXT,
    results_json TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at REAL NOT NULL,
    updated_at REAL,
    last_run_id TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_conv_idx ON chat_messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation ON chat_messages(conversation_id, id);
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
        self._migrate()
        self.conn.commit()

    # columns added after the first release; existing databases get them on open
    MIGRATIONS = [
        ("queue_items", "project_id", "TEXT"),
        ("queue_items", "milestone", "INTEGER"),
        ("queue_items", "project_key", "TEXT"),
        ("queue_items", "merged_sha", "TEXT"),
        ("queue_items", "project_processed", "INTEGER NOT NULL DEFAULT 0"),
        ("projects", "previous_spec", "TEXT"),
        ("queue_items", "max_runtime_s", "INTEGER"),
        ("queue_items", "seed_run_id", "TEXT"),
        ("runs", "seed_run_id", "TEXT"),
        ("runs", "max_runtime_s", "INTEGER"),
        ("runs", "max_repair_cycles", "INTEGER"),
        ("runs", "heal_of", "TEXT"),
        ("runs", "heal_attempt", "INTEGER"),
        ("runs", "issue_body", "TEXT"),
        ("runs", "variant_id", "TEXT"),
        ("queue_items", "issue_url", "TEXT"),
        ("queue_items", "queue", "TEXT"),
        ("queue_items", "heal_attempts", "INTEGER"),
        ("runs", "archived", "INTEGER NOT NULL DEFAULT 0"),
        ("chat_messages", "tool_calls", "TEXT"),
    ]

    def _migrate(self) -> None:
        for table, col, typ in self.MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

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

    def set_archived(self, run_id: str, archived: bool) -> None:
        self.update_run(run_id, archived=1 if archived else 0)

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

    # -------------------------------------------------------------- failures
    FAILURE_FIELDS = ("title", "repo", "state", "reason", "category", "root_cause", "fix", "lesson", "retry",
                      "diagnosed_by", "evidence", "note_stem", "heal_run_id", "outcome", "cost_usd", "elapsed_s")

    def failure_save(self, run_id: str, **fields: Any) -> None:
        bad = set(fields) - set(self.FAILURE_FIELDS)
        if bad:
            raise ValueError(f"unknown failure fields {bad}")
        if self.failure(run_id):
            self.failure_update(run_id, **fields)
            return
        cols = ", ".join(["run_id", "created_at", *fields])
        marks = ", ".join("?" * (len(fields) + 2))
        self._exec(f"INSERT INTO failures ({cols}) VALUES ({marks})", (run_id, time.time(), *fields.values()))

    def failure_update(self, run_id: str, **fields: Any) -> None:
        bad = set(fields) - set(self.FAILURE_FIELDS)
        if bad:
            raise ValueError(f"unknown failure fields {bad}")
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._exec(f"UPDATE failures SET {sets} WHERE run_id = ?", (*fields.values(), run_id))

    def failure(self, run_id: str) -> dict | None:
        return self._one("SELECT * FROM failures WHERE run_id = ?", (run_id,))

    def failures(self, limit: int = 500) -> list[dict]:
        return self._all("SELECT * FROM failures ORDER BY created_at DESC LIMIT ?", (limit,))

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

    # ------------------------------------------------------------------ queue
    QUEUE_FIELDS = ("title", "body", "repo", "mode", "budget", "max_cycles", "chain", "approval",
                    "status", "run_id", "base_branch", "result_branch", "note", "started_at",
                    "finished_at", "position", "max_runtime_s", "seed_run_id", "heal_attempts", "issue_url",
                    "queue", "project_id", "milestone", "project_key", "merged_sha", "project_processed")

    def queue_add(self, title: str, body: str, repo: str, **fields: Any) -> int:
        row = self._one("SELECT MAX(position) AS m FROM queue_items")
        pos = (row["m"] or 0) + 1
        fields = {"mode": "swarm", "chain": 1, "approval": "yes", **fields}
        fields["queue"] = (fields.get("queue") or "main").strip() or "main"
        cols = ", ".join(["position", "title", "body", "repo", "created_at", *fields])
        qs = ", ".join(["?"] * (5 + len(fields)))
        return self._exec(f"INSERT INTO queue_items ({cols}) VALUES ({qs})",
                          (pos, title, body, repo, time.time(), *fields.values())).lastrowid

    def queue_items(self, include_finished: bool = True) -> list[dict]:
        if include_finished:
            return self._all("SELECT * FROM queue_items ORDER BY position")
        return self._all("SELECT * FROM queue_items WHERE status IN ('queued','running') ORDER BY position")

    def queue_names(self) -> list[str]:
        """Every queue ("pile") that has items, plus 'main'; most recently used first."""
        rows = self._all("SELECT COALESCE(queue, 'main') AS q, MAX(created_at) AS t FROM queue_items GROUP BY q "
                         "ORDER BY t DESC")
        names = [r["q"] for r in rows]
        return names if "main" in names else names + ["main"]

    def queue_item(self, item_id: int) -> dict | None:
        return self._one("SELECT * FROM queue_items WHERE id = ?", (item_id,))

    def queue_update(self, item_id: int, **fields: Any) -> None:
        bad = set(fields) - set(self.QUEUE_FIELDS)
        if bad:
            raise ValueError(f"unknown queue fields {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE queue_items SET {sets} WHERE id = ?", (*fields.values(), item_id))

    RUN_TABLES = ("tasks", "events", "llm_calls", "test_runs", "findings", "acceptance", "interventions",
                  "approvals")

    def delete_run(self, run_id: str) -> None:
        """Remove a run and everything recorded about it (its failure post-mortem is kept for learning)."""
        if self._one("SELECT id FROM queue_items WHERE run_id=? AND project_id IS NOT NULL", (run_id,)):
            raise ValueError("Project runs retain their evidence and budget history; archive instead")
        with self._lock:
            for t in self.RUN_TABLES:
                self.conn.execute(f"DELETE FROM {t} WHERE run_id = ?", (run_id,))
            self.conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            self.conn.commit()

    # ---------------------------------------------------------- recent repos
    RECENT_REPOS_KEY = "recent_repos"

    def recent_repos(self, limit: int = 15) -> list[str]:
        import json as _json
        try:
            saved = _json.loads(self.kv_get(self.RECENT_REPOS_KEY) or "[]")
        except ValueError:
            saved = []
        if not saved:  # first use: learn from history
            rows = self._all("SELECT repo, MAX(t) AS t FROM (SELECT repo, created_at AS t FROM queue_items "
                             "UNION ALL SELECT repo, created_at AS t FROM runs) WHERE repo IS NOT NULL "
                             "AND repo != '' GROUP BY repo ORDER BY t DESC LIMIT ?", (limit,))
            saved = [r["repo"] for r in rows]
        return [r for r in saved if r][:limit]

    def remember_repo(self, repo: str, limit: int = 15) -> None:
        import json as _json
        repo = (repo or "").strip()
        if not repo:
            return
        items = [repo] + [r for r in self.recent_repos(limit) if r != repo]
        self.kv_set(self.RECENT_REPOS_KEY, _json.dumps(items[:limit]))

    def forget_repo(self, repo: str) -> None:
        import json as _json
        self.kv_set(self.RECENT_REPOS_KEY, _json.dumps([r for r in self.recent_repos(100) if r != repo] or [""]))

    def queue_remove(self, item_id: int) -> None:
        if (self.queue_item(item_id) or {}).get("project_id"):
            raise ValueError("Project tasks retain their planning and budget history; cancel the project instead")
        self._exec("DELETE FROM queue_items WHERE id = ? AND status != 'running'", (item_id,))

    def queue_move(self, item_id: int, direction: int) -> None:
        """Swap an item with its neighbour among not-yet-started items of the same queue (-1 up, +1 down)."""
        me = self.queue_item(item_id)
        lane = (me or {}).get("queue") or "main"
        items = [i for i in self.queue_items() if i["status"] == "queued" and (i.get("queue") or "main") == lane]
        idx = next((n for n, i in enumerate(items) if i["id"] == item_id), None)
        if idx is None:
            return
        j = idx + direction
        if 0 <= j < len(items):
            a, b = items[idx], items[j]
            with self._lock:
                self.conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (b["position"], a["id"]))
                self.conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (a["position"], b["id"]))
                self.conn.commit()

    def queue_clear_finished(self) -> int:
        return self._exec("DELETE FROM queue_items WHERE project_id IS NULL AND status IN ('done','failed','cancelled','skipped')").rowcount

    # ------------------------------------------------------------- variants
    def variant_add(self, vid: str, overlay: dict, parent: str | None = None, rationale: str = "") -> None:
        self._exec("INSERT INTO variants (id, parent, created_at, overlay, rationale) VALUES (?, ?, ?, ?, ?)",
                   (vid, parent, time.time(), json.dumps(overlay), rationale))

    def variant(self, vid: str) -> dict | None:
        row = self._one("SELECT * FROM variants WHERE id = ?", (vid,))
        return _variant_row(row) if row else None

    def variants(self) -> list[dict]:
        return [_variant_row(r) for r in self._all("SELECT * FROM variants ORDER BY created_at")]

    def variant_update(self, vid: str, **fields: Any) -> None:
        if "scores" in fields:
            fields["scores"] = json.dumps(fields["scores"])
        if "overlay" in fields:
            fields["overlay"] = json.dumps(fields["overlay"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE variants SET {sets} WHERE id = ?", (*fields.values(), vid))

    # ------------------------------------------------- chat conversations
    def conversation_create(self, conv_id: str, title: str | None = None) -> dict:
        self._exec("INSERT OR IGNORE INTO conversations (id, title, created_at) VALUES (?, ?, ?)",
                   (conv_id, title, time.time()))
        conv = self.conversation_get(conv_id)
        assert conv is not None
        return conv

    def conversation_get(self, conv_id: str) -> dict | None:
        return self._one("SELECT * FROM conversations WHERE id = ?", (conv_id,))

    def conversations_list(self) -> list[dict]:
        return self._all(
            "SELECT * FROM conversations ORDER BY COALESCE(updated_at, created_at) DESC")

    def conversation_delete(self, conv_id: str) -> None:
        self._exec("DELETE FROM chat_messages WHERE conversation_id = ?", (conv_id,))
        self._exec("DELETE FROM conversations WHERE id = ?", (conv_id,))

    def conversation_append(self, conv_id: str, role: str, content: str) -> int:
        self._exec("INSERT OR IGNORE INTO conversations (id, created_at) VALUES (?, ?)",
                   (conv_id, time.time()))
        cur = self._exec(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (conv_id, role, content, time.time()))
        self._exec("UPDATE conversations SET updated_at = ? WHERE id = ?", (time.time(), conv_id))
        return int(cur.lastrowid)

    def conversation_messages(self, conv_id: str, limit: int | None = None) -> list[dict]:
        if limit is None:
            return self._all(
                "SELECT id, conversation_id, role, content, created_at FROM chat_messages "
                "WHERE conversation_id = ? ORDER BY id ASC", (conv_id,))
        rows = self._all(
            "SELECT id, conversation_id, role, content, created_at FROM chat_messages "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?", (conv_id, limit))
        return sorted(rows, key=lambda r: r["id"])

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str | None) -> None:
        if value is None:
            self._exec("DELETE FROM kv WHERE key = ?", (key,))
        else:
            self._exec("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                       (key, value))

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


def _unlock_readonly(func, path, _exc):  # git objects are read-only on Windows
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def dir_size_bytes(path: Path) -> int:
    """Recursive sum of file sizes under path; missing path -> 0; never raises."""
    total = 0
    try:
        for p in Path(path).rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def free_run_disk(run_dir: Path) -> int:
    """Delete <run_dir>/repo and <run_dir>/worktrees, keep everything else.

    Returns the number of bytes freed (0 if nothing was there)."""
    run_dir = Path(run_dir)
    freed = 0
    for name in ("repo", "worktrees"):
        target = run_dir / name
        if target.is_dir():
            freed += dir_size_bytes(target)
            shutil.rmtree(target, onerror=_unlock_readonly)
    return freed


def _variant_row(row: dict) -> dict:
    d = dict(row)
    d["overlay"] = json.loads(d.get("overlay") or "{}")
    d["scores"] = json.loads(d.get("scores") or "[]")
    return d
