"""Durable decision journal and exclusive project-lead turns, not a second worker."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..execution import transaction
from .spec import ProjectError, fixed

if TYPE_CHECKING:
    from ..db import Store

FINAL = {"applied", "blocked", "rejected", "failed", "superseded"}


def encoded(value: Any, limit: int = 64000) -> str:
    raw = json.dumps(value, sort_keys=True, default=str)
    if len(raw) > limit:
        clipped = {"truncated": True, "sha256": hashlib.sha256(raw.encode()).hexdigest(), "preview": raw[:limit]}
        if isinstance(value, dict) and "_policy" in value:
            clipped["_policy"] = value["_policy"]
        raw = json.dumps(clipped)
    return raw


def epoch(store: Store, project: dict) -> str:
    event = store._one("SELECT COALESCE(MAX(id),0) AS id FROM project_events WHERE project_id=? "
                       "AND kind IN ('approval','merge','spike_finished','reply','spec_approved')", (project["id"],))
    last = store._one("SELECT COALESCE(MAX(id),0) AS id FROM queue_items WHERE project_id=? AND milestone=? "
                      "AND status IN ('done','failed')", (project["id"], project["current_milestone"]))
    source = [project["id"], fixed(project["spec"]), project["current_milestone"], event["id"], last["id"]]
    return hashlib.sha256(encoded(source).encode()).hexdigest()


@dataclass
class LeadTurn:
    store: Store
    project_id: str
    token: str
    decision_id: int | None = None

    @classmethod
    def claim(cls, store: Store, project_id: str) -> LeadTurn | None:
        from ..launcher import pid_alive
        token = uuid.uuid4().hex
        with transaction(store) as db:
            p = db.execute("SELECT status,approved_at FROM projects WHERE id=?", (project_id,)).fetchone()
            if not p or p["status"] != "running" or not p["approved_at"]:
                return None
            if db.execute("SELECT id FROM queue_items WHERE project_id=? AND status='running' LIMIT 1", (project_id,)).fetchone():
                return None
            old = db.execute("SELECT * FROM project_lead_claims WHERE project_id=?", (project_id,)).fetchone()
            if old:
                if pid_alive(old["owner_pid"]):
                    return None
                unfinished = db.execute("SELECT id FROM project_decisions WHERE project_id=? AND status IN ('proposed','validated','executing')", (project_id,)).fetchall()
                db.execute("DELETE FROM project_lead_claims WHERE project_id=?", (project_id,))
                if unfinished:
                    reason = "Project lead worker was lost. Inspect its saved decision and repository state before resuming; no action was replayed."
                    db.execute("UPDATE project_decisions SET status='failed',finished_at=?,result=? WHERE project_id=? AND status IN ('proposed','validated','executing')",
                               (time.time(), encoded({"error": reason}), project_id))
                    db.execute("UPDATE projects SET status='waiting_for_you',last_error=? WHERE id=?", (reason, project_id))
                    db.execute("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)",
                               (project_id, time.time(), "lead_recovery", encoded({"reason": reason, "decision_ids": [r["id"] for r in unfinished]})))
                    return None
            db.execute("INSERT INTO project_lead_claims VALUES (?,?,?,?)", (project_id, token, os.getpid(), time.time()))
        return cls(store, project_id, token)

    def release(self) -> None:
        self.store._exec("DELETE FROM project_lead_claims WHERE project_id=? AND token=?", (self.project_id, self.token))

    def begin(self, cycle: str, evidence: dict, max_actions: int) -> int | None:
        with transaction(self.store) as db:
            owner = db.execute("SELECT token FROM project_lead_claims WHERE project_id=?", (self.project_id,)).fetchone()
            if not owner or owner["token"] != self.token:
                raise ProjectError("Lead turn lost its ownership claim")
            count = db.execute("SELECT COUNT(*) FROM project_decisions WHERE project_id=? AND epoch=?", (self.project_id, cycle)).fetchone()[0]
            if count >= max_actions:
                return None
            cur = db.execute("INSERT INTO project_decisions(project_id,epoch,ordinal,created_at,action,status,evidence) VALUES (?,?,?,?,?,'proposed',?)",
                             (self.project_id, cycle, count + 1, time.time(), "propose", encoded(evidence)))
            self.decision_id = cur.lastrowid
        return self.decision_id

    def candidate(self, value: Any, *, action: str, reason: str) -> None:
        self.store._exec("UPDATE project_decisions SET proposal=?,action=?,reason=? WHERE id=? AND status='proposed'",
                         (encoded(value), action, reason[:8000], self.decision_id))

    def mark(self, status: str, result: Any = None, *, connection: Any = None) -> None:
        if self.decision_id is None:
            return
        with (transaction(self.store) if connection is None else nullcontext(connection)) as db:
            row = db.execute("SELECT status,action,reason FROM project_decisions WHERE id=?", (self.decision_id,)).fetchone()
            if not row or row["status"] in FINAL:
                return
            db.execute("UPDATE project_decisions SET status=?,result=?,finished_at=? WHERE id=?",
                       (status, encoded(result), time.time() if status in FINAL else None, self.decision_id))
            if status in FINAL:
                db.execute("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)",
                           (self.project_id, time.time(), "lead_decision", encoded({"decision_id": self.decision_id,
                            "action": row["action"], "status": status, "reason": row["reason"], "result": result}, limit=3000)))


def evidence_catalog(store: Store, project: dict, snapshot: dict, items: list[dict]) -> dict:
    evidence: dict[str, Any] = {"repository:" + snapshot["sha"]: {"repo": project["repo"], "sha": snapshot["sha"]}}
    selected = {q["id"]: q for q in items[-30:]}
    selected.update({q["id"]: q for q in items if q["status"] in ("queued", "tentative", "running")})
    for q in selected.values():
        evidence[f"task:{q['id']}"] = {"id": q["id"], "key": q["project_key"], "status": q["status"], "run_id": q.get("run_id")}
        if q.get("run_id"):
            run = store.get_run(q["run_id"])
            if run:
                evidence["run:" + run["id"]] = {"id": run["id"], "state": run["state"], "reason": run.get("state_reason")}
    for i, check in enumerate(project["definition"]["milestones"][project["current_milestone"]]["checks"]):
        evidence[f"check:{i + 1}"] = {"text": check, "milestone": project["current_milestone"]}
    return evidence


async def inspect(store: Store, project: dict, repository: Any, snapshot: dict, paths: tuple[str, ...],
                  run_ids: tuple[str, ...]) -> dict:
    import asyncio
    results: dict[str, Any] = {"sha": snapshot["sha"], "files": {}, "runs": {}}
    excerpt_limit = min(6000, 24000 // max(1, len(paths)))
    for name in paths:
        if not hasattr(repository, "read"):
            raise ProjectError("Repository adapter does not support bounded file inspection")
        content, blob = await asyncio.wait_for(asyncio.to_thread(repository.read, name, snapshot["sha"]), timeout=30)
        if blob is None:
            raise ProjectError("Inspection path not found in the approved repository: " + name)
        results["files"][name] = {"blob": blob, "sha256": hashlib.sha256(content.encode()).hexdigest(),
                                  "excerpt": content[:excerpt_limit], "truncated": len(content) > excerpt_limit}
    for rid in run_ids:
        run = store.get_run(rid)
        tests = [{key: str(t.get(key))[:500] if isinstance(t.get(key), str) else t.get(key)
                  for key in ("id", "label", "command", "exit_code", "passed", "failed", "timed_out")} for t in store.test_runs(rid)[-3:]]
        findings = [{key: str(f.get(key) or "")[:500] for key in ("severity", "file", "title", "description")}
                    for f in store.findings(rid)[-6:]]
        results["runs"][rid] = {"state": run["state"], "reason": str(run.get("state_reason") or "")[:1000],
                                "tests": tests, "findings": findings}
    return results


async def choose_action(cfg, store, project, repository, snapshot, items, context, ask, transport,
                        turn: LeadTurn, validate, pause):
    """Bounded model proposals followed by deterministic validation/execution.

    Returns the existing plan/checkpoint/escalation shape for the publication path.
    Read-only inspections can feed one subsequent proposal; all steps count durably.
    """
    import asyncio
    from . import lead
    from .actions import ActionKind, LeadAction, validate_scope
    from .store import Projects
    ps = Projects(store)
    cycle = epoch(store, project)
    catalog = evidence_catalog(store, project, snapshot, items)
    for saved in store._all("SELECT id,result FROM project_decisions WHERE project_id=? AND epoch=? AND action='inspect' AND status='applied'", (project["id"], cycle)):
        result = json.loads(saved["result"] or "{}")
        if result.get("findings"):
            catalog[f"inspection:{saved['id']}"] = result["findings"]
    first = store._one("SELECT evidence FROM project_decisions WHERE project_id=? AND epoch=? ORDER BY ordinal LIMIT 1", (project["id"], cycle))
    policy = json.loads(first["evidence"])["_policy"] if first else {
        "max_actions": cfg.projects.max_lead_actions, "budget_usd": cfg.projects.lead_budget_usd,
        "timeout_s": cfg.projects.lead_timeout_s,
        "initial_cost": store.cost_summary("project:" + project["id"])["total"]}
    context["evidence_catalog"] = catalog
    def queue_manifest(rows):
        fields = ("id", "status", "position", "body", "budget", "mode", "project_key", "depends_on", "project_processed", "merged_sha", "execution_id")
        return encoded([{key: row.get(key) for key in fields} for row in rows])
    expected_queue = queue_manifest(items)
    invalid = 0
    while True:
        used = store.cost_summary("project:" + project["id"])["total"] - policy["initial_cost"]
        ms = ps.milestones(project["id"])[project["current_milestone"]]
        available = min(policy["budget_usd"] - used, project["budget_usd"] - ps.spent(project["id"]),
                        ms["estimate_usd"] * cfg.projects.escalate_budget_factor - ps.spent(project["id"], project["current_milestone"]))
        if available <= 0 or turn.begin(cycle, {**catalog, "_policy": policy}, policy["max_actions"]) is None:
            pause("The project lead reached its decision allowance. Review the saved decisions before continuing.", {"epoch": cycle})
            return None
        context["decision_budget_usd"] = available
        context["actions_remaining"] = policy["max_actions"] - store._one("SELECT COUNT(*) AS n FROM project_decisions WHERE project_id=? AND epoch=?", (project["id"], cycle))["n"]
        last_call = store._one("SELECT COALESCE(MAX(id),0) AS id FROM llm_calls WHERE run_id=?", ("project:" + project["id"],))["id"]
        try:
            result = await asyncio.wait_for(ask(cfg, store, project, lead.LEAD, context, transport), timeout=policy["timeout_s"])
        except BaseException as exc:
            # A provider may have accepted an interrupted request. Retain the reserved
            # decision allowance as an estimate rather than silently replenishing it.
            known = store._one("SELECT id FROM llm_calls WHERE run_id=? AND id>? AND status='ok' AND cost_usd IS NOT NULL LIMIT 1", ("project:" + project["id"], last_call))
            if not known:
                store.record_llm_call("project:" + project["id"], role="lead_reservation", model="uncertain",
                                      task_key=f"project-lead:m{project['current_milestone']}", cost_usd=available,
                                      cost_source="estimated", status="uncertain", error=str(exc)[:500])
            turn.mark("failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise
        turn.candidate(result, action=str(result.get("action", "invalid")) if isinstance(result, dict) else "invalid",
                       reason=str(result.get("reason", "")) if isinstance(result, dict) else "Invalid response")
        current = ps.get(project["id"])
        unknown = store._one("SELECT id FROM llm_calls WHERE run_id=? AND id>? AND status='ok' AND cost_usd IS NULL LIMIT 1", ("project:" + project["id"], last_call))
        if unknown:
            store.record_llm_call("project:" + project["id"], role="lead_reservation", model="unpriced",
                                  task_key=f"project-lead:m{project['current_milestone']}", cost_usd=available,
                                  cost_source="estimated", status="uncertain", error="Successful decision request had no spending information")
            if current["status"] == "running":
                pause("The lead response had no spending information. Review its recorded allowance before continuing.",
                      {"decision_id": turn.decision_id}, decision_status="blocked",
                      decision_result={"error": "Decision spending could not be verified; retained its conservative allowance"})
            else:
                turn.mark("superseded", {"reason": "Project state changed during an unpriced decision"})
            return None
        if current["status"] != "running" or current["spec"] != project["spec"] or queue_manifest(ps.items(project["id"])) != expected_queue:
            turn.mark("superseded", {"reason": "Human pause, spec revision or queue changes superseded this proposal"})
            return None
        try:
            action = LeadAction.parse(result, "repository:" + snapshot["sha"])
            validate_scope(action, project, items, catalog, max_runtime_s=cfg.limits.max_runtime_s,
                           investigation_timeout_s=cfg.projects.investigation_timeout_s)
            converted = action.legacy(project["spec"])
            spec = validate(project, converted, cfg.projects.horizon) if action.kind in (
                ActionKind.PLAN, ActionKind.INVESTIGATE, ActionKind.REVIEW, ActionKind.EVALUATE, ActionKind.ESCALATE) else None
            if action.kind == ActionKind.EVALUATE and (not any(q.get("merged_sha") and q["milestone"] == project["current_milestone"] for q in items)
                                                       or any(c["status"] == "failed" for c in context["verification"])):
                raise ProjectError("Milestone completion requires merged work and no failed checks")
            if action.kind == ActionKind.INSPECT:
                identity = encoded({"paths": sorted(action.paths), "runs": sorted(action.run_ids)})
                previous = store._all("SELECT proposal FROM project_decisions WHERE project_id=? AND epoch=? AND action='inspect' AND status='applied'", (project["id"], cycle))
                if any(encoded({"paths": sorted(json.loads(r["proposal"]).get("paths", [])), "runs": sorted(json.loads(r["proposal"]).get("run_ids", []))}) == identity for r in previous):
                    raise ProjectError("This inspection already succeeded; use its saved evidence or change approach")
                turn.mark("validated")
                findings = await inspect(store, project, repository, snapshot, action.paths, action.run_ids)
                ref = f"inspection:{turn.decision_id}"
                catalog[ref] = findings
                context["evidence_catalog"] = catalog
                turn.mark("applied", {"evidence_ref": ref, "paths": list(action.paths), "run_ids": list(action.run_ids), "sha": snapshot["sha"], "findings": findings})
                continue
            if action.kind in (ActionKind.PAUSE, ActionKind.QUESTION, ActionKind.BROWSER):
                if action.kind == ActionKind.BROWSER:
                    message = "Browser journey verification is required for: " + "; ".join(action.checks) + ". No browser journey runner is installed in this stage; verify these checks or resume with a correction."
                    status = "blocked"
                else:
                    message, status = action.question or action.reason, "applied"
                pause(message, {"decision_id": turn.decision_id, "action": action.kind.value}, decision_status=status,
                      decision_result={"message": message, "evidence": list(action.evidence), "verification": "unverified" if action.kind == ActionKind.BROWSER else None})
                return None
            turn.mark("validated", {"evidence": list(action.evidence)})
            return action, converted, spec
        except (ProjectError, TypeError, ValueError) as exc:
            turn.mark("rejected", {"error": str(exc)})
            ps.event(project["id"], "proposal_rejected", {"reason": str(exc), "decision_id": turn.decision_id})
            context["rejected_proposal"] = result
            context["correction_required"] = str(exc)
            invalid += 1
            if invalid >= 2:
                raise ProjectError("Lead returned two invalid proposals. Retry after correcting the model configuration.") from exc
