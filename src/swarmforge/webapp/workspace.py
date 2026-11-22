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
    tasks = Store.tasks
    list_runs = Store.list_runs
    failure = Store.failure
    benchmarks = Store.benchmarks

    def __init__(self, conn):
        self.conn = conn

    def _one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]


class WorkspaceReader(WorkReader):
    def kv(self, key):
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

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
            messages = self._messages(conn, cid)
            requests = self._chat_requests(conn, cid, messages)
            return {"conversation": dict(row), "messages": messages, "costs": ReadStore(conn).cost_summary("chat:" + cid), "requests": requests}

    @staticmethod
    def _chat_requests(conn, cid, messages):
        """Recent background replies (status, tool steps, failures) and attachments on their messages."""
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_requests'").fetchone():
            return []
        rows = [dict(r) for r in conn.execute("SELECT id,status,substr(question,1,2000) AS question,steps,substr(error,1,1000) AS error,started_at,finished_at,user_message_id,attachments "
                                              "FROM chat_requests WHERE conversation_id=? ORDER BY started_at DESC LIMIT 5", (cid,))]
        by_message = {}
        for r in rows:
            try:
                r["steps"] = [s for s in json.loads(r["steps"] or "[]") if isinstance(s, dict)][:40]
                r["attachments"] = [a for a in json.loads(r["attachments"] or "[]") if isinstance(a, dict)][:6]
            except ValueError:
                r["steps"], r["attachments"] = [], []
            message_id = r.pop("user_message_id")
            if message_id is not None:
                by_message[message_id] = r["attachments"]
        all_rows = conn.execute("SELECT user_message_id,attachments FROM chat_requests WHERE conversation_id=? AND user_message_id IS NOT NULL AND attachments!='[]' ORDER BY started_at DESC LIMIT 100", (cid,))
        for message_id, attachments in all_rows:
            if message_id not in by_message:
                try:
                    by_message[message_id] = [a for a in json.loads(attachments) if isinstance(a, dict)][:6]
                except ValueError:
                    pass
        for m in messages:
            m["attachments"] = by_message.get(m["id"], [])
        return rows

    def chat_upload(self, cid, upload_id):
        """Path of an image attached to a message in this conversation, or None."""
        with self.connection() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_uploads'").fetchone():
                return None
            row = conn.execute("SELECT u.path,u.kind,u.name FROM chat_uploads u JOIN chat_requests r ON r.id=u.request_id "
                               "WHERE u.id=? AND r.conversation_id=?", (upload_id, cid)).fetchone()
            return dict(row) if row else None

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


FAILURE_FIELDS = "run_id,created_at,title,repo,state,category,substr(root_cause,1,2000) AS root_cause,outcome,cost_usd,heal_run_id"


def _work_item(conn, run_id):
    if not run_id:
        return None
    row = conn.execute("SELECT id FROM queue_items WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    return f"task-{row[0]}" if row else f"run-{run_id}"


def _failure_rows(conn, rows):
    from ..postmortem import CATEGORIES
    items = []
    for r in rows:
        item = dict(r)
        item["category_label"] = CATEGORIES.get(item["category"] or "", item["category"] or "Not categorised")
        item["work_item"] = _work_item(conn, item["run_id"])
        items.append(item)
    return items


def failures(reader, *, limit=25, offset=0, category=""):
    """Post-mortems for failed runs, newest first, with outcome counts per cause."""
    with reader.connection() as conn:
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='failures'").fetchone():
            return {"items": [], "total": 0, "limit": limit, "offset": offset, "counts": {"total": 0, "fixed": 0, "needs_you": 0}, "categories": [], "undiagnosed": 0}
        where, args = (" WHERE category=?", (category,)) if category else ("", ())
        rows = conn.execute("SELECT " + FAILURE_FIELDS + " FROM failures" + where + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
        items = _failure_rows(conn, rows)
        total = conn.execute("SELECT COUNT(*) FROM failures" + where, args).fetchone()[0]
        counts = conn.execute("SELECT COUNT(*), SUM(outcome LIKE 'fixed itself%'), "
                              "SUM(outcome LIKE 'needs you%' OR outcome LIKE 'still failing%' OR outcome LIKE 'not retried%') FROM failures").fetchone()
        from ..postmortem import CATEGORIES
        cats = [{"id": c or "", "label": CATEGORIES.get(c or "", c or "Not categorised"), "count": n}
                for c, n in conn.execute("SELECT category,COUNT(*) FROM failures GROUP BY category ORDER BY COUNT(*) DESC LIMIT 50")]
        undiagnosed = conn.execute("SELECT COUNT(*) FROM runs WHERE state IN ('FAILED','BLOCKED','BUDGET_EXCEEDED') "
                                   "AND id NOT IN (SELECT run_id FROM failures)").fetchone()[0]
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "counts": {"total": counts[0] or 0, "fixed": counts[1] or 0, "needs_you": counts[2] or 0},
                "categories": cats, "undiagnosed": undiagnosed}


def failure(reader, run_id):
    with reader.connection() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='failures'").fetchone():
            return None
        row = conn.execute("SELECT " + FAILURE_FIELDS + ",substr(reason,1,4000) AS reason,substr(fix,1,8000) AS fix,substr(lesson,1,8000) AS lesson,"
                           "substr(evidence,1,16000) AS evidence,length(evidence)>16000 AS evidence_truncated,diagnosed_by FROM failures WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        item = _failure_rows(conn, [row])[0]
        item["evidence_truncated"] = bool(item["evidence_truncated"])
        item["heal_work_item"] = _work_item(conn, item["heal_run_id"])
        return item


def speed(reader, limit=30):
    """Where finished runs spent their time (Streamlit Speed page), computed read-only."""
    from .. import metrics as mx
    from ..services.healing import healing_summary
    with reader.connection() as conn:
        conn.execute("BEGIN")
        store = ReadStore(conn)
        rows = mx.collect(store, limit)
        agents = mx.agent_rows(store, limit)
        summary = mx.summarize(rows) if rows else {"all": {}, "by_settings": {}}
        groups = {}
        for a in agents:
            g = groups.setdefault((a.get("role"), a.get("model")), {"role": a.get("role"), "model": a.get("model"), "minutes": [], "per_call": [], "retries": 0})
            g["minutes"].append(a.get("minutes") or 0)
            if a.get("sec_per_call") is not None:
                g["per_call"].append(a["sec_per_call"])
            g["retries"] += a.get("retries") or 0
        def median(xs):
            xs = sorted(xs)
            return (xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2) if xs else None
        by_role = sorted(({"role": g["role"], "model": g["model"], "sessions": len(g["minutes"]), "median_min": median(g["minutes"]),
                           "max_min": max(g["minutes"]), "median_sec_per_call": median(g["per_call"]), "retries": g["retries"]}
                          for g in groups.values()), key=lambda g: -(g["median_min"] or 0))
        clean = lambda d: {k: (v if isinstance(v, (int, float, str)) or v is None else str(v)) for k, v in d.items()}
        return {"phases": mx.PHASES, "phase_help": mx.PHASE_HELP, "summary": clean(summary["all"]),
                "by_settings": [{"settings": k, **clean(v)} for k, v in summary["by_settings"].items()][:50],
                "agents": by_role[:100], "runs": [clean({k: v for k, v in r.items() if k != "started"}) for r in rows][:200],
                "healing": [clean(h) for h in healing_summary(store)][:50]}


def benchmarks(reader):
    from ..evaluation import summarize
    with reader.connection() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='benchmarks'").fetchone():
            return {"items": []}
        items = []
        for row in ReadStore(conn).benchmarks()[:50]:
            results = row.get("results") or []
            entry = {"id": row.get("id"), "label": row.get("label"), "issue": row.get("issue"), "suite": False, "rows": []}
            if results and isinstance(results, list) and all(isinstance(r, dict) and "case" in r for r in results):
                entry["suite"] = True
                try:
                    summary = summarize(results)
                except (KeyError, TypeError, ValueError, ZeroDivisionError, AttributeError):
                    entry["error"] = "This suite's recorded results are incomplete and cannot be summarised."
                    summary = {}
                for key, s in summary.items():
                    team, _, mode = key.rpartition(" / ")
                    entry["rows"].append({"team": team or "(default)", "mode": mode, "runs": s["runs"], "successful": s["successful"],
                                          "delivery_pct": round(100 * s["delivery_rate"]), "median_min": round(s["median_elapsed_s"] / 60, 1),
                                          "p95_min": round(s["p95_elapsed_s"] / 60, 1), "cost_usd": s["cost_usd"],
                                          "cost_per_success_usd": s["cost_per_success_usd"], "repair_cycles": s["repair_cycles"],
                                          "unpriced_calls": s["unpriced_calls"]})
            elif isinstance(results, list):
                for r in results[:20]:
                    if isinstance(r, dict):
                        entry["rows"].append({str(k): (v if isinstance(v, (int, float, str, bool)) or v is None else str(v)) for k, v in list(r.items())[:30]})
            items.append(entry)
        return {"items": items}


def settings_details(config):
    """Editable settings for the React Settings page: never key values, only whether keys are set."""
    import hashlib as _h
    import os
    if config is None:
        return {"configured": False}
    source = getattr(config, "source_path", None)
    sha = None
    if source and Path(source).is_file():
        sha = _h.sha256(Path(source).read_bytes()).hexdigest()
    keys = ("budget_usd", "max_runtime_s", "max_concurrent_agents", "max_repair_cycles")
    def view(cfg):
        return ([{"role": name, "provider": r.provider, "model": r.model, "temperature": float(r.temperature),
                  "max_steps": int(r.max_steps)} for name, r in cfg.roles.items()],
                {k: getattr(cfg.limits, k) for k in keys})
    running = view(config)
    saved = running
    if sha is not None:
        # Show what is saved in the file; the running service keeps the values it started with.
        try:
            from ..config import parse_config
            from ..settings import read_raw
            saved = view(parse_config(read_raw(source), source_path=Path(source)))
        except Exception:  # noqa: BLE001 - an unparseable file still shows the running values
            saved = running
    return {"configured": True, "editable": sha is not None, "config_sha": sha, "source_name": Path(source).name if source else None,
            "roles": saved[0], "limits": saved[1], "restart_needed": saved != running,
            "providers": [{"name": name, "key_env": p.api_key_env, "key_set": bool(os.environ.get(p.api_key_env))}
                          for name, p in config.providers.items()]}
