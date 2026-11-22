"""Read-only, bounded projections shared by the web API and future clients.

Opening the dashboard must never migrate or create a runtime database. Only
explicit public fields are selected; configuration and model transcripts stay private.
"""
from __future__ import annotations

import sqlite3
import re
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

    def list(self, *, limit=25, offset=0, status=None, search="", view="all"):
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise ValueError("Invalid page bounds")
        where, args = [], []
        views = {"all": "1=1", "prs": "pr_url IS NOT NULL AND pr_url<>''",
                 "failures": "status='failed'", "history": "status IN ('done','failed','cancelled','skipped')"}
        if view not in views:
            raise ValueError("Unknown Work view")
        where.append(views[view])
        if status:
            where.append("status=?")
            args.append(status)
        if search:
            where.append("(instr(lower(title),lower(?))>0 OR instr(lower(COALESCE(repo,'')),lower(?))>0)")
            args.extend([search, search])
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connection() as conn:
            # One read transaction keeps total, counts and page internally consistent.
            conn.execute("BEGIN")
            base = "WITH work AS (" + self.projection + ") "
            total = conn.execute(base + "SELECT COUNT(*) FROM work" + clause, args).fetchone()[0]
            counts = {r[0]: r[1] for r in conn.execute(base + "SELECT status,COUNT(*) FROM work GROUP BY status")}
            rows = conn.execute(base + "SELECT * FROM work" + clause +
                                " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 "
                                "WHEN 'tentative' THEN 2 ELSE 3 END,created_at DESC,id DESC LIMIT ? OFFSET ?", [*args, limit, offset])
            return {"items": [dict(r) for r in rows], "total": total, "counts": counts,
                    "limit": limit, "offset": offset}

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
            return dict(row) if row else None

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
                      "events": [], "next_event_id": after, "events_has_more": False}
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
