"""Project persistence. Writes spanning queue, project and events are transactional."""
from __future__ import annotations

import json
import time
import uuid

from .spec import ProjectError, render, validate


class Projects:
    def __init__(self, store):
        self.store = store

    def get(self, pid):
        p = self.store._one("SELECT * FROM projects WHERE id=?", (pid,))
        if not p:
            raise ProjectError("Project not found")
        p["definition"] = json.loads(p["definition"])
        return p

    def list(self):
        return [self.get(p["id"]) for p in self.store._all("SELECT id FROM projects ORDER BY created_at DESC")]

    def create(self, name="New project", *, repo="", greeting=None):
        pid = uuid.uuid4().hex[:12]
        cid = "project-" + pid
        with self.store.transaction():  # project, its conversation and the greeting exist together or not at all
            self.store.conversation_create(cid, name)
            self.store._exec("INSERT INTO projects (id,name,repo,conversation_id,budget_usd,created_at,spec,definition) VALUES (?,?,?,?,?,?,?,?)",
                             (pid, name, repo, cid, 0, time.time(), "", "{}"))
            self._greet(cid, greeting)
        return self.get(pid)

    def _greet(self, cid, greeting):
        self.store.conversation_append(cid, "assistant", greeting or "Who is this app for, and what are the 3–5 things they must be able to do? We will turn those into milestones, then agree on scope, constraints, budget and done-when checks before launch.")

    def draft(self, pid, definition, *, expected_spec=None):
        d = validate(definition)
        with self.store.transaction():
            p = self.get(pid)
            if p["status"] != "drafting":
                raise ProjectError("Only a draft can be edited before launch")
            if expected_spec is not None:
                import hashlib
                if expected_spec != hashlib.sha256(p["spec"].encode()).hexdigest():
                    raise ProjectError("The spec changed. Reload and review before saving.")
            self.store.conn.execute("UPDATE projects SET name=?,repo=?,budget_usd=?,spec=?,definition=? WHERE id=? AND status='drafting'",
                                    (d["name"], d["repo"].lower(), d["budget_usd"], render(d), json.dumps(d), pid))

    def items(self, pid):
        return self.store._all("SELECT * FROM queue_items WHERE project_id=? ORDER BY position", (pid,))

    def milestones(self, pid):
        return self.store._all("SELECT * FROM milestones WHERE project_id=? ORDER BY idx", (pid,))

    def event(self, pid, kind, detail):
        self.store._exec("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)", (pid, time.time(), kind, json.dumps(detail)))

    def events(self, pid):
        return self.store._all("SELECT * FROM project_events WHERE project_id=? ORDER BY id", (pid,))

    def decisions(self, pid, limit=30):
        rows = self.store._all("SELECT * FROM project_decisions WHERE project_id=? ORDER BY id DESC LIMIT ?", (pid, min(100, max(1, limit))))
        for row in rows:
            for key in ("proposal", "evidence", "result"):
                row[key] = json.loads(row[key]) if row[key] else None
        return rows

    def update(self, pid, **kw):
        allowed = {"status", "spec", "observed_sha", "current_milestone", "last_error", "approved_at", "previous_spec"}
        if not kw or set(kw) - allowed:
            raise ProjectError("Invalid project update")
        self.store._exec("UPDATE projects SET " + ",".join(f"{k}=?" for k in kw) + " WHERE id=?", (*kw.values(), pid))

    def spent(self, pid, milestone=None):
        items = self.items(pid)
        run_ids = {q["run_id"] for q in items if q.get("run_id") and (milestone is None or q["milestone"] == milestone)}
        # Include every self-healing attempt, not just the last run attached to the queue row.
        for rid in list(run_ids):
            run_ids.update(r["id"] for r in self.store._all("SELECT id FROM runs WHERE heal_of=?", (rid,)))
            r = self.store.get_run(rid)
            while r and r.get("heal_of") and r["heal_of"] not in run_ids:
                run_ids.add(r["heal_of"])
                r = self.store.get_run(r["heal_of"])
        # Lost-worker continuations and held-PR replacements share a durable task
        # allowance even when the queue row now points only at the newest attempt.
        for rid in list(run_ids):
            run = self.store.get_run(rid)
            if run and run.get("execution_id"):
                run_ids.update(r["id"] for r in self.store._all("SELECT id FROM runs WHERE execution_id=?", (run["execution_id"],)))
        if milestone is None:
            lead = self.store.cost_summary("project:" + pid)["total"]
        else:
            lead = self.store._one("SELECT COALESCE(SUM(cost_usd),0) AS cost FROM llm_calls WHERE run_id=? AND task_key=?",
                                   ("project:" + pid, f"project-lead:m{milestone}"))["cost"]
        return lead + sum(self.store.cost_summary(r)["total"] for r in run_ids)
