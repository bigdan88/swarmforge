"""Public, bounded workspace projections. No writable Store is opened for browsing."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from ..db import Store
from ..projects.store import Projects
from .queries import WorkReader


class ReadStore:
    get_run = Store.get_run
    cost_summary = Store.cost_summary

    def __init__(self, conn):
        self.conn = conn

    def _one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]


class WorkspaceReader(WorkReader):
    project_fields = "id,name,repo,status,current_milestone,budget_usd,created_at,approved_at,last_error,conversation_id"

    def projects(self, *, limit=25, offset=0, search=""):
        with self.connection() as conn:
            conn.execute("BEGIN")
            ps = Projects(ReadStore(conn))
            where = " WHERE instr(lower(name || ' ' || repo),lower(?))>0"
            rows = conn.execute("SELECT " + self.project_fields + " FROM projects" + where + " ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (search, limit, offset))
            items = [dict(r) for r in rows]
            for item in items:
                item["spent_usd"] = ps.spent(item["id"])
            total = conn.execute("SELECT COUNT(*) FROM projects" + where, (search,)).fetchone()[0]
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def project(self, pid):
        with self.connection() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT " + self.project_fields + ",spec,definition FROM projects WHERE id=?", (pid,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["spent_usd"] = Projects(ReadStore(conn)).spent(pid)
            spec = item.pop("spec")
            definition = item.pop("definition")
            return {"project": item, "spec": spec[:256000], "spec_sha": hashlib.sha256(spec.encode()).hexdigest(),
                    "definition": json.loads(definition) if len(definition) <= 64000 else {},
                    "truncated": len(spec) > 256000 or len(definition) > 64000,
                    "milestones": [dict(r) for r in conn.execute("SELECT idx,name,status,estimate_usd,spent_usd,started_at,finished_at,substr(report,1,16000) AS report FROM milestones WHERE project_id=? ORDER BY idx LIMIT 6", (pid,))],
                    "tasks": [dict(r) for r in conn.execute(self.queue_projection + " WHERE q.project_id=? ORDER BY q.position LIMIT 100", (pid,))],
                    "events": [dict(r) for r in conn.execute("SELECT id,ts,kind,substr(detail,1,12000) AS detail FROM project_events WHERE project_id=? ORDER BY id DESC LIMIT 50", (pid,))],
                    "messages": self._messages(conn, item["conversation_id"])}

    @staticmethod
    def _messages(conn, cid):
        rows = [dict(r) for r in conn.execute("SELECT id,role,substr(content,1,32000) AS content,created_at,length(content)>32000 AS truncated FROM chat_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 100", (cid,))]
        return list(reversed(rows))

    def conversations(self, *, limit=25, offset=0):
        with self.connection() as conn:
            rows = conn.execute("SELECT id,title,created_at,updated_at FROM conversations WHERE id NOT LIKE 'project-%' ORDER BY COALESCE(updated_at,created_at) DESC,id LIMIT ? OFFSET ?", (limit, offset))
            return {"items": [dict(r) for r in rows], "total": conn.execute("SELECT COUNT(*) FROM conversations WHERE id NOT LIKE 'project-%'").fetchone()[0], "limit": limit, "offset": offset}

    def conversation(self, cid):
        with self.connection() as conn:
            row = conn.execute("SELECT id,title,created_at,updated_at FROM conversations WHERE id=? AND id NOT LIKE 'project-%'", (cid,)).fetchone()
            if not row:
                return None
            return {"conversation": dict(row), "messages": self._messages(conn, cid), "costs": ReadStore(conn).cost_summary("chat:" + cid)}

    def controls(self, item_id):
        query = self._detail_query(item_id)
        if query is None:
            return None
        with self.connection() as conn:
            item = conn.execute(*query).fetchone()
            if not item:
                return None
            project = None
            if item_id.startswith("task-"):
                project = conn.execute("SELECT project_id FROM queue_items WHERE id=?", (int(item_id[5:]),)).fetchone()[0]
            return {"can_cancel": item["status"] in ("queued", "running", "tentative"),
                    "can_retry": importlib.util.find_spec("swarmforge.execution") is not None and item_id.startswith("task-") and not project and item["status"] in ("done", "failed", "cancelled", "skipped"),
                    "project_id": project,
                    "approvals": [dict(r) for r in conn.execute("SELECT id,substr(question,1,4000) AS question,length(question)>4000 OR instr(question,char(0))>0 AS truncated FROM approvals WHERE run_id=? AND status='pending' ORDER BY id LIMIT 20", (item["run_id"],))]}

    def queue_status(self):
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key='queue_paused'").fetchone()
            worker = conn.execute("SELECT value FROM kv WHERE key='queue_worker_pid'").fetchone()
            from ..launcher import pid_alive
            try:
                running = bool(worker and pid_alive(int(worker[0])))
            except ValueError:
                running = False
            return {"paused": bool(row and row[0] == "1"), "worker_running": running,
                    "lanes": [dict(r) for r in conn.execute("SELECT COALESCE(queue,'main') AS name,status,COUNT(*) AS count FROM queue_items WHERE status IN ('queued','running','tentative') GROUP BY name,status LIMIT 100")]}

    def outcomes(self):
        with self.connection() as conn:
            items = [dict(r) for r in conn.execute("""SELECT r.mode,COUNT(*) AS runs,
                SUM(r.state='COMPLETED') AS completed,SUM(r.state IN ('FAILED','BLOCKED','BUDGET_EXCEEDED')) AS failed,
                COALESCE(SUM(c.cost),0) AS cost_usd FROM runs r LEFT JOIN
                (SELECT run_id,SUM(cost_usd) AS cost FROM llm_calls WHERE cost_source IN ('provider_reported','estimated') GROUP BY run_id) c
                ON c.run_id=r.id GROUP BY r.mode LIMIT 20""")]
            return {"items": items, "total": len(items), "limit": 20, "offset": 0}


def public_settings(config):
    if config is None:
        return {"configured": False, "roles": [], "limits": {}, "team": None, "teams": []}
    try:
        from ..teams import list_teams
        teams = [{"name": name, "description": str(t.get("description", ""))[:1000], "models": t.get("models", {})} for name, t in list_teams(config).items() if not t.get("error")][:30]
    except ImportError:
        teams = []
    return {"configured": True, "team": getattr(config, "team", None), "teams": teams,
            "roles": [{"role": name, "model": role.model, "fallbacks": getattr(role, "fallback_models", [])} for name, role in config.roles.items()],
            "limits": {k: getattr(config.limits, k) for k in ("budget_usd", "max_runtime_s", "max_concurrent_agents", "max_parallel_queues", "max_repair_cycles") if hasattr(config.limits, k)}}


def feature_history():
    return json.loads((Path(__file__).parents[1] / "resources/feature-history.json").read_text(encoding="utf-8"))
