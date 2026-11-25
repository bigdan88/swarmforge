"""Read-only, bounded projections shared by the web API and future clients.

Opening the dashboard must never migrate or create a runtime database. Only
explicit public fields are selected; configuration and model transcripts stay private.
"""
from __future__ import annotations

import sqlite3
import re
import json
from contextlib import contextmanager
from pathlib import Path


class WorkReader:
    def __init__(self, database: Path):
        self.database = database.resolve()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            yield conn
        finally:
            conn.close()

    # Include direct runs, but not a second copy of a run attached to a queue item.
    queue_projection = """
        SELECT 'task-' || q.id AS id, q.title, q.repo, q.status,
               r.state AS stage, q.run_id, q.created_at, r.updated_at,
               r.finished_at, r.state_reason AS reason, r.pr_url
        FROM queue_items q LEFT JOIN runs r ON r.id=q.run_id
    """
    run_projection = """
        SELECT 'run-' || r.id AS id, COALESCE(r.issue_title, r.id) AS title, r.repo,
               CASE WHEN r.state='COMPLETED' THEN 'done'
                    WHEN r.state='CANCELLED' THEN 'cancelled'
                    WHEN r.state IN ('FAILED','BLOCKED','BUDGET_EXCEEDED') THEN 'failed'
                    WHEN r.finished_at IS NULL THEN 'running' ELSE 'failed' END AS status,
               r.state AS stage, r.id AS run_id, r.created_at, r.updated_at, r.finished_at,
               r.state_reason AS reason, r.pr_url
        FROM runs r
    """
    # Build the attachment set once, instead of scanning queue history for each run.
    # Excluding NULL is essential: a single undispatched task must not hide all runs.
    projection = queue_projection + " UNION ALL " + run_projection + """
        WHERE r.id NOT IN (SELECT run_id FROM queue_items WHERE run_id IS NOT NULL)
    """

    def list(self, *, limit=25, offset=0, status=None, search="", view="all", archived=False, fold=False):
        """One bounded page of work. Archived runs are hidden unless asked for. With fold, attempts that
        share persisted lineage (the shared task allowance, or a retry/heal/continue chain) collapse into
        their most relevant row (running, then waiting, then newest) with an attempt count. Titles are
        never used: independent tasks with the same title stay separate, and legacy rows without
        recorded lineage stay individually visible."""
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise ValueError("Invalid page bounds")
        where, args = [], []
        views = {"all": "1=1", "prs": "pr_url IS NOT NULL AND pr_url<>''",
                 "failures": "status='failed'", "history": "status IN ('done','failed','cancelled','skipped')"}
        if view not in views:
            raise ValueError("Unknown Work view")
        where.append(views[view])
        if not archived:
            where.append("archived=0")
        if status:
            where.append("status=?")
            args.append(status)
        if search:
            where.append("(instr(lower(title),lower(?))>0 OR instr(lower(COALESCE(repo,'')),lower(?))>0)")
            args.extend([search, search])
        clause = " WHERE " + " AND ".join(where) if where else ""
        rank = "CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'tentative' THEN 2 ELSE 3 END"
        with self.connection() as conn:
            # One read transaction keeps total, counts and page internally consistent.
            conn.execute("BEGIN")
            run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            queue_cols = {r[1] for r in conn.execute("PRAGMA table_info(queue_items)")}
            flag = "COALESCE((SELECT archived FROM runs WHERE id=b.run_id),0)" if "archived" in run_cols else "0"
            base = "WITH RECURSIVE " + (self._lineage_cte(run_cols) + ", " if fold else "")
            base += ("raw AS (" + self.projection + "), work AS (SELECT b.*, " + flag + " AS archived FROM raw b), "
                     "picked AS (SELECT * FROM work" + clause + ")")
            if fold:
                base += (", keyed AS (SELECT *, " + self._lineage_key(run_cols, queue_cols) + " AS fold_key FROM picked), "
                         "ranked AS (SELECT *, COUNT(*) OVER (PARTITION BY fold_key) AS attempts, ROW_NUMBER() OVER "
                         "(PARTITION BY fold_key ORDER BY " + rank + ",COALESCE(finished_at,updated_at,created_at) DESC,created_at DESC,id DESC) AS fold_rank FROM keyed), "
                         "shown AS (SELECT * FROM ranked WHERE fold_rank=1) ")
            else:
                base += ", shown AS (SELECT *, 1 AS attempts FROM picked) "
            total = conn.execute(base + "SELECT COUNT(*) FROM shown", args).fetchone()[0]
            attempts_total = conn.execute(base + "SELECT COUNT(*) FROM picked", args).fetchone()[0]
            counts = {r[0]: r[1] for r in conn.execute("WITH work AS (" + self.projection + ") SELECT status,COUNT(*) FROM work GROUP BY status")}
            rows = conn.execute(base + "SELECT id,title,repo,status,stage,run_id,created_at,updated_at,finished_at,reason,pr_url,"
                                "archived,attempts FROM shown ORDER BY " + rank + ",created_at DESC,id DESC LIMIT ? OFFSET ?",
                                [*args, limit, offset])
            items = [dict(r) for r in rows]
            for item in items:
                item["archived"] = bool(item["archived"])
            return {"items": items, "total": total, "counts": counts, "limit": limit, "offset": offset,
                    "attempts_total": attempts_total}

    @staticmethod
    def _lineage_cte(run_cols):
        """lineage(id, root): every run mapped to the first run of its task. Runs with a shared
        allowance use it directly (its id is the root run); legacy runs follow heal/retry/continue
        parents. Depth is bounded so a corrupt cycle cannot loop."""
        parents = [c for c in ("heal_of", "retry_of", "seed_run_id") if c in run_cols]
        parent = "COALESCE(" + ",".join(f"r.{c}" for c in parents) + ")" if len(parents) > 1 else (f"r.{parents[0]}" if parents else "NULL")
        own = "COALESCE(r.execution_id, r.id)" if "execution_id" in run_cols else "r.id"
        has_exec = "r.execution_id IS NOT NULL" if "execution_id" in run_cols else "0"
        return ("lineage(id, root, depth) AS (SELECT r.id, " + own + ", 0 FROM runs r WHERE " + parent + " IS NULL OR " + has_exec +
                " UNION ALL SELECT r.id, l.root, l.depth+1 FROM runs r JOIN lineage l ON " + parent + "=l.id"
                " WHERE NOT (" + has_exec + ") AND l.depth<50)")

    @staticmethod
    def _lineage_key(run_cols, queue_cols):
        root = "(SELECT root FROM lineage WHERE id={} LIMIT 1)"
        queue = "(SELECT {} FROM queue_items WHERE id=CAST(substr(keyed_src.id,6) AS INTEGER))"
        parts = []
        if "execution_id" in queue_cols:
            parts.append("CASE WHEN keyed_src.id LIKE 'task-%' THEN " + queue.format("execution_id") + " END")
        parts.append(root.format("keyed_src.run_id"))
        if "retry_of" in queue_cols:  # a queued retry that has not started yet
            parts.append("CASE WHEN keyed_src.id LIKE 'task-%' THEN " + root.format(queue.format("retry_of")) + " END")
        return ("COALESCE(" + ", ".join(parts) + ", keyed_src.id)").replace("keyed_src.", "picked.")

    def _detail_query(self, item_id):
        # Look up the actual primary key; filtering a concatenated public ID over
        # the union forces both histories to be scanned for every selected task.
        if re.fullmatch(r"task-[1-9][0-9]{0,18}", item_id):
            key = int(item_id[5:])
            if key > 9223372036854775807:
                return None
            query = self.queue_projection + " WHERE q.id=?"
        elif item_id.startswith("run-") and item_id[4:]:
            key = item_id[4:]
            query = self.run_projection + """ WHERE r.id=?
                AND NOT EXISTS (SELECT 1 FROM queue_items q WHERE q.run_id=r.id)"""
        else:
            return None
        return query, (key,)

    def detail(self, item_id):
        query = self._detail_query(item_id)
        if query is None:
            return None
        with self.connection() as conn:
            row = conn.execute(*query).fetchone()
            if not row:
                return None
            item = dict(row)
            item["archived"], item["attempts"] = False, 1
            if item["run_id"] and any(r[1] == "archived" for r in conn.execute("PRAGMA table_info(runs)")):
                flag = conn.execute("SELECT archived FROM runs WHERE id=?", (item["run_id"],)).fetchone()
                item["archived"] = bool(flag and flag[0])
            return item

    def evidence(self, item_id, *, after=0, limit=50):
        """Bounded database evidence only: never open run paths or raw event data.

        after=0 starts with the most recent events. Thereafter an event ID cursor
        catches up in order, including across reconnects; unlike Work offsets it
        cannot skip new events when history is inserted ahead of a page.
        """
        if not 0 <= after <= 9223372036854775807 or not 1 <= limit <= 100:
            raise ValueError("Invalid evidence bounds")
        query = self._detail_query(item_id)
        if query is None:
            return None
        with self.connection() as conn:
            conn.execute("BEGIN")
            item = conn.execute(*query).fetchone()
            if item is None:
                return None
            run_id = item["run_id"]
            result = {"item_id": item_id, "run_id": run_id, "costs": None, "allowance": None,
                      "checks": [], "checks_has_more": False, "tests": [], "tests_has_more": False,
                      "events": [], "next_event_id": after, "events_has_more": False, "execution": None}
            if run_id is None:
                return result
            result["costs"] = dict(conn.execute("""
                SELECT COUNT(*) AS calls,
                COALESCE(SUM(CASE WHEN cost_source='provider_reported' THEN cost_usd END),0) AS reported_usd,
                COALESCE(SUM(CASE WHEN cost_source='estimated' THEN cost_usd END),0) AS estimated_usd,
                COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_source IS NULL OR
                    cost_source NOT IN ('provider_reported','estimated') THEN 1 ELSE 0 END),0) AS unavailable_calls
                FROM llm_calls WHERE run_id=?""", (run_id,)).fetchone())
            result["allowance"] = self._allowance(conn, run_id)
            result["execution"] = self._execution(conn, run_id)
            checks = [dict(r) for r in conn.execute("""
                SELECT id,cycle,substr(criterion_id,1,200) AS criterion_id,substr(text,1,4000) AS text,
                       substr(status,1,100) AS status,substr(evidence,1,4000) AS evidence,
                       COALESCE(length(text)>4000 OR length(evidence)>4000 OR length(criterion_id)>200,0) AS truncated
                FROM acceptance WHERE run_id=? ORDER BY id DESC LIMIT 101""", (run_id,))]
            result["checks"], result["checks_has_more"] = checks[:100], len(checks) > 100
            tests = [dict(r) for r in conn.execute("""
                SELECT id,ts,cycle,substr(label,1,500) AS label,exit_code,passed,failed,errors,skipped,timed_out
                FROM test_runs WHERE run_id=? ORDER BY id DESC LIMIT 26""", (run_id,))]
            result["tests"], result["tests_has_more"] = tests[:25], len(tests) > 25
            direction = "ASC" if after else "DESC"
            events = [dict(r) for r in conn.execute(f"""
                SELECT id,ts,substr(level,1,100) AS level,substr(kind,1,200) AS kind,
                       substr(message,1,4000) AS message,COALESCE(length(message)>4000,0) AS truncated
                FROM events WHERE run_id=? AND id>? ORDER BY id {direction} LIMIT ?""", (run_id, after, limit + 1))]
            result["events_has_more"] = bool(after and len(events) > limit)
            result["events"] = events[:limit] if after else list(reversed(events[:limit]))
            if result["events"]:
                result["next_event_id"] = result["events"][-1]["id"]
            return result

    @staticmethod
    def _execution(conn, run_id):
        """Public task graph and observed model usage, never configuration or prompts.

        Aggregate counts cover the whole run even when the bounded task preview is full.
        Phase history is independent of the recent-event cursor so reconnects cannot
        make an earlier stage look as though it never happened.
        """
        run = conn.execute("SELECT mode,repair_cycle FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:  # Legacy queue rows can reference a run whose record is unavailable.
            return None
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT state,COUNT(*) FROM tasks WHERE run_id=? GROUP BY state", (run_id,))}
        tasks = [dict(r) for r in conn.execute("""
            SELECT substr(task_key,1,200) AS task_key,cycle,kind,substr(title,1,300) AS title,substr(role,1,100) AS role,
                   substr(model,1,200) AS model,state,substr(depends_on,1,65537) AS depends_on,
                   substr(expected_files,1,65537) AS expected_files,
                   started_at,ended_at,attempts,substr(summary,1,1200) AS summary,substr(error,1,1200) AS error,
                   COALESCE(length(summary)>1200 OR length(error)>1200 OR length(title)>300,0) AS truncated
            FROM tasks WHERE run_id=? ORDER BY cycle DESC,id DESC LIMIT 201""", (run_id,))]
        has_more = len(tasks) > 200
        tasks = list(reversed(tasks[:200]))
        for task in tasks:
            for field, limit, width in (("depends_on", 50, 200), ("expected_files", 20, 240)):
                raw = task[field]
                try:
                    values = json.loads(raw or "[]") if len(raw or "") <= 65536 else None
                except (ValueError, TypeError, RecursionError):
                    values = None
                valid = isinstance(values, list) and all(isinstance(v, str) for v in values)
                clipped = not valid or len(values) > limit or any(len(v) > width for v in values)
                task[field] = [v[:width] for v in values[:limit]] if valid else []
                task["truncated"] = bool(task["truncated"] or clipped)
                if field == "depends_on":
                    task["dependencies_complete"] = not clipped
            task["attempts"] = task["attempts"] or 0
        bots = [dict(r) for r in conn.execute("""
            SELECT substr(role,1,100) AS role,substr(provider,1,100) AS provider,substr(model,1,200) AS model,
                   COUNT(*) AS calls,COALESCE(SUM(total_tokens),0) AS tokens,
                   COALESCE(SUM(CASE WHEN cost_source IN ('provider_reported','estimated') THEN cost_usd END),0) AS known_cost_usd,
                   SUM(CASE WHEN cost_usd IS NULL OR cost_source IS NULL OR
                       cost_source NOT IN ('provider_reported','estimated') THEN 1 ELSE 0 END) AS unavailable_calls,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                   MIN(started_at) AS first_call_at,MAX(ended_at) AS last_call_at
            FROM llm_calls WHERE run_id=? GROUP BY role,provider,model
            ORDER BY calls DESC,role,model,provider LIMIT 41""", (run_id,))]
        history = [dict(r) for r in conn.execute("""
            SELECT id,ts,substr(message,1,1000) AS message,substr(data,1,16000) AS data
            FROM events WHERE run_id=? AND kind='state' ORDER BY id DESC LIMIT 101""", (run_id,))]
        phases = []
        for event in reversed(history[:100]):
            try:
                data = json.loads(event["data"] or "{}")
            except (ValueError, TypeError, RecursionError):
                data = {}
            state = data.get("to") if isinstance(data, dict) else None
            if not isinstance(state, str):
                match = re.search(r"->\s*([A-Z_]+)", event["message"] or "")
                state = match[1] if match else None
            known_states = {"ISSUE_RECEIVED", "REPOSITORY_INSPECTION", "PLANNING", "TASK_VALIDATION",
                            "TASK_EXECUTION", "INTEGRATION", "TESTING", "CODE_REVIEW", "FIX_REQUIRED",
                            "APPROVED", "PULL_REQUEST_CREATION", "COMPLETED", "FAILED", "BLOCKED",
                            "CANCELLED", "BUDGET_EXCEEDED"}
            if state in known_states:
                phases.append({"id": event["id"], "ts": event["ts"], "state": state[:100]})
        return {"mode": run["mode"], "repair_cycle": run["repair_cycle"] or 0,
                "counts": counts, "tasks": tasks, "tasks_has_more": has_more,
                "bots": bots[:40], "bots_has_more": len(bots) > 40,
                "phases": phases, "phases_has_more": len(history) > 100}

    @staticmethod
    def _allowance(conn, run_id):
        # Older databases have no execution policy tables. Reading them must not
        # create an allowance or imply that an old per-run budget is shared.
        required = {"execution_attempts", "execution_allowances", "execution_calls"}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            return None
        row = conn.execute("""
            SELECT t.number AS attempt,a.budget_usd,a.spent_usd,a.deadline,t.last_progress_at,
                   substr(t.current_action,1,1000) AS current_action,substr(t.last_result,1,1000) AS last_result,
                   substr(t.next_action,1,1000) AS next_action,substr(t.stop_reason,1,1000) AS stop_reason,
                   COALESCE((SELECT SUM(c.reserved_usd) FROM execution_calls c
                       WHERE c.execution_id=a.id AND c.status='reserved'),0) AS reserved_usd
            FROM execution_attempts t JOIN execution_allowances a ON a.id=t.execution_id
            WHERE t.run_id=?""", (run_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["available_usd"] = max(0, value["budget_usd"] - value["spent_usd"] - value["reserved_usd"])
        return value
