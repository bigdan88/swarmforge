"""Durable decision journal and exclusive project-lead turns, not a second worker."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .. import jobs
from ..execution import transaction
from .spec import ProjectError, fixed
from .autonomy import milestone_limit

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


def queue_manifest(rows):
    fields = ("id", "status", "position", "body", "budget", "mode", "project_key", "depends_on", "project_processed", "merged_sha", "execution_id", "max_runtime_s")
    return encoded([{key: row.get(key) for key in fields} for row in rows])


def epoch(store: Store, project: dict) -> str:
    event = store._one("SELECT COALESCE(MAX(id),0) AS id FROM project_events WHERE project_id=? "
                       "AND kind IN ('approval','merge','spike_finished','reply','spec_approved','automatic_recovery','verification_repair','milestone_progressed')", (project["id"],))
    last = store._one("SELECT COALESCE(MAX(id),0) AS id FROM queue_items WHERE project_id=? AND milestone=? "
                      "AND status IN ('done','failed')", (project["id"], project["current_milestone"]))
    source = [project["id"], fixed(project["spec"]), project["current_milestone"], event["id"], last["id"]]
    return hashlib.sha256(encoded(source).encode()).hexdigest()


LEAD_JOB = "project_lead"
LOST = "Project lead worker was lost. Inspect its saved decision and repository state before resuming; no action was replayed."


def _on_lead_transition(db, job: dict, to_state: str, reason: str) -> None:
    """A lead turn whose owner vanished may have been mid-action: stop for a human, replay nothing."""
    if to_state not in ("interrupted", "uncertain"):
        return
    _fail_unfinished(db, job["subject"])


def _fail_unfinished(db, project_id: str) -> bool:
    unfinished = db.execute("SELECT id,status FROM project_decisions WHERE project_id=? AND status IN ('proposed','validated','executing')", (project_id,)).fetchall()
    if not unfinished:
        return False
    db.execute("UPDATE project_decisions SET status='failed',finished_at=?,result=? WHERE project_id=? AND status IN ('proposed','validated','executing')",
               (time.time(), encoded({"error": LOST}), project_id))
    project = db.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
    was_running = bool(project and project["status"] == "running")
    if was_running:
        db.execute("UPDATE projects SET status='waiting_for_you',last_error=? WHERE id=?", (LOST, project_id))
    db.execute("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)",
               (project_id, time.time(), "lead_recovery", encoded({"reason": LOST, "decision_ids": [r["id"] for r in unfinished],
                  "safe_to_retry": was_running and all(r["status"] == "validated" for r in unfinished)})))
    return True


jobs.register(LEAD_JOB, on_transition=_on_lead_transition, recover_to="uncertain")


def lead_active(db, project_id: str) -> bool:
    """True while a lead turn holds the project (a running job, or a claim left by an older version)."""
    if db.execute("SELECT 1 FROM jobs WHERE kind=? AND subject=? AND state IN ('pending','running')", (LEAD_JOB, project_id)).fetchone():
        return True
    return db.execute("SELECT 1 FROM project_lead_claims WHERE project_id=?", (project_id,)).fetchone() is not None


@dataclass
class LeadTurn:
    store: Store
    project_id: str
    job_id: str
    decision_id: int | None = None

    @classmethod
    def claim(cls, store: Store, project_id: str) -> LeadTurn | None:
        from ..launcher import pid_alive
        jobs.ensure_tables(store)
        jobs.recover(store, LEAD_JOB, project_id)  # a turn whose owner is gone stops for a human first
        with transaction(store) as db:
            p = db.execute("SELECT status,approved_at FROM projects WHERE id=?", (project_id,)).fetchone()
            if not p or p["status"] != "running" or not p["approved_at"]:
                return None
            if db.execute("SELECT id FROM queue_items WHERE project_id=? AND status='running' LIMIT 1", (project_id,)).fetchone():
                return None
            legacy = db.execute("SELECT * FROM project_lead_claims WHERE project_id=?", (project_id,)).fetchone()
            if legacy:  # written by a version before jobs
                if pid_alive(legacy["owner_pid"]):
                    return None
                db.execute("DELETE FROM project_lead_claims WHERE project_id=?", (project_id,))
                if _fail_unfinished(db, project_id):
                    return None
            try:
                job = jobs.create(store, LEAD_JOB, project_id)
            except jobs.JobBusy:
                return None
        return cls(store, project_id, job["id"])

    def keep_alive(self):
        """Renew this turn's lease while the lead works (model calls can take minutes)."""
        return jobs.keep_alive(self.store, self.job_id)

    def release(self, *, failed: bool = False, reason: str = "") -> None:
        jobs.finish(self.store, self.job_id, "failed" if failed else "succeeded", error=reason or None,
                    reason=reason or "lead turn ended")

    def begin(self, cycle: str, evidence: dict, max_actions: int) -> int | None:
        with transaction(self.store) as db:
            owner = db.execute("SELECT state,owner FROM jobs WHERE id=?", (self.job_id,)).fetchone()
            if not owner or owner["state"] != "running" or owner["owner"] != jobs.ME.instance:
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
    from .actions import ActionKind, LeadAction, existing_task_reference, validate_scope
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
    context["min_build_budget_usd"] = cfg.projects.min_build_budget_usd
    context["autonomous"] = cfg.projects.autonomous
    from .autonomy import instructions
    context["automatic_recovery"] = instructions(store, project["id"])
    expected_queue = queue_manifest(items)
    context["existing_ready_tasks"] = [{key: q.get(key) for key in ("id", "project_key", "title", "budget", "mode", "max_runtime_s")}
                                       for q in items if q["status"] == "queued" and q["milestone"] == project["current_milestone"]]
    context["action_examples"] = {
        "continue_existing": {"action": "continue_plan", "reason": "The next accepted task remains appropriate", "evidence": ["repository:" + snapshot["sha"]],
                              "task_id": context["existing_ready_tasks"][0]["id"] if context["existing_ready_tasks"] else "integer ID of the next queued task"},
        "new_investigation": {"action": "investigate", "reason": "Resolve an in-scope uncertainty", "evidence": ["repository:" + snapshot["sha"]],
                              "task": {"key": "probe", "title": "Investigate the uncertainty", "description": "State hypothesis and observable result here",
                                       "acceptance_criteria": ["Retain concrete findings"], "files": ["docs/research.md"], "ready": True,
                                       "kind": "spike", "budget_usd": min(2, project["budget_usd"] * .1), "depends_on": [], "max_runtime_s": min(cfg.limits.max_runtime_s, cfg.projects.investigation_timeout_s)}}}
    invalid = 0
    while True:
        used = store.cost_summary("project:" + project["id"])["total"] - policy["initial_cost"]
        ms = ps.milestones(project["id"])[project["current_milestone"]]
        available = min(policy["budget_usd"] - used, project["budget_usd"] - ps.spent(project["id"]),
                        milestone_limit(cfg, project, ms) - ps.spent(project["id"], project["current_milestone"]))
        if available <= 0 or turn.begin(cycle, {**catalog, "_policy": policy}, policy["max_actions"]) is None:
            from .autonomy import schedule_repair
            if schedule_repair(cfg, store, project, "planning", "Repair the invalid planning response using the saved exact errors and valid action examples. Choose a materially different approach if the same proposal failed repeatedly. Last rejection: " + str(context.get("correction_required") or "decision round exhausted")[:2500]):
                return None
            detail = context.get("correction_required")
            message = (f"Automatic planning repair used its bounded decision allowance after {invalid} rejected responses. "
                       f"Last rejection: {detail}. Use Retry planning to try again within the remaining project allowance."
                       if detail else "The project lead reached its decision allowance. Review the saved decisions before continuing.")
            pause(message, {"epoch": cycle, "rejections": invalid, "last_rejection": detail, "recovery_kind": "planning"})
            return None
        context["decision_budget_usd"] = available
        context["actions_remaining"] = policy["max_actions"] - store._one("SELECT COUNT(*) AS n FROM project_decisions WHERE project_id=? AND epoch=?", (project["id"], cycle))["n"]
        last_call = store._one("SELECT COALESCE(MAX(id),0) AS id FROM llm_calls WHERE run_id=?", ("project:" + project["id"],))["id"]
        response_error = None
        # A long project gives the lead a long context; after each timeout since the last applied
        # decision, allow twice as long (up to 20 min) instead of failing the same way again.
        timeouts = store._one("SELECT COUNT(*) AS n FROM project_decisions WHERE project_id=? AND status='failed' AND result LIKE '%TimeoutError%' "
                              "AND id > (SELECT COALESCE(MAX(id),0) FROM project_decisions WHERE project_id=? AND status='applied')",
                              (project["id"], project["id"]))["n"]
        timeout_s = min(max(policy["timeout_s"], 1200), policy["timeout_s"] * 2 ** min(timeouts, 4))
        if timeouts:
            context["_lead_timeouts"], context["_lead_time_limit_s"] = timeouts, timeout_s
        else:
            context.pop("_lead_timeouts", None)
            context.pop("_lead_time_limit_s", None)
        try:
            result = await asyncio.wait_for(ask(cfg, store, project, lead.LEAD, context, transport), timeout=timeout_s)
        except ProjectError as exc:
            # Invalid JSON is a known, side-effect-free response failure. Its call
            # was already charged by the model client; repair it inside this cap.
            response_error = str(exc)
            result = {"action": "invalid_response", "reason": "The lead response was not valid action JSON"}
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
            if response_error:
                raise ProjectError("The lead response could not be decoded as one JSON action: " + response_error)
            candidate, normalization = existing_task_reference(result, project, items)
            action = LeadAction.parse(candidate, "repository:" + snapshot["sha"])
            adjusted = tuple(replace(task, budget_usd=max(task.budget_usd, cfg.projects.min_build_budget_usd))
                             if task.kind == "build" else task for task in action.tasks)
            if adjusted != action.tasks:
                action = replace(action, tasks=adjusted)
                normalization = (normalization + " " if normalization else "") + f"Applied the configured ${cfg.projects.min_build_budget_usd:g} minimum build-task allowance; project and milestone caps still apply."
            ordered, remaining_tasks = [], list(action.tasks)
            keys = {task.key for task in action.tasks}
            while remaining_tasks:
                eligible = [task for task in remaining_tasks if not (set(task.depends_on) & keys) - {t.key for t in ordered}]
                if not eligible:
                    break  # scope validation below reports cycles; nothing is silently dropped
                task = next((t for t in eligible if t.ready), eligible[0])
                ordered.append(task)
                remaining_tasks.remove(task)
            if not remaining_tasks and tuple(ordered) != action.tasks:
                action = replace(action, tasks=tuple(ordered))
                normalization = (normalization + " " if normalization else "") + "Ordered tasks after their prerequisites without changing scope or readiness."
            validate_scope(action, project, items, catalog, max_runtime_s=cfg.limits.max_runtime_s,
                           investigation_timeout_s=cfg.projects.investigation_timeout_s)
            merged_work = any(q.get("merged_sha") and q["milestone"] <= project["current_milestone"] for q in items)
            if action.kind == ActionKind.BROWSER and cfg.projects.autonomous and merged_work and not any(c["status"] == "failed" for c in context["verification"]):
                action = replace(action, kind=ActionKind.EVALUATE)
                normalization = (normalization + " " if normalization else "") + "Kept unavailable browser observations unverified for the final preview review."
            converted = ({"action": "continue_plan", "task_id": action.task_id, "queue_manifest": expected_queue, "normalization": normalization}
                         if action.kind == ActionKind.CONTINUE else action.legacy(project["spec"]))
            spec = validate(project, converted, cfg.projects.horizon) if action.kind in (
                ActionKind.PLAN, ActionKind.INVESTIGATE, ActionKind.REVIEW, ActionKind.EVALUATE, ActionKind.ESCALATE) else None
            from .autonomy import deferral_allowed
            if action.kind == ActionKind.EVALUATE and (not merged_work or (
                    any(c["status"] == "failed" for c in context["verification"]) and not deferral_allowed(store, project))):
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
                from .autonomy import schedule_repair
                if schedule_repair(cfg, store, project, "clarification", "The owner wants a working preview with minimal interruption. Before asking, try an in-scope alternative, a reasonable reversible assumption, or fixtures for unavailable services/devices. Keep permissions, actual physical observations and unavailable credentials unverified. If none can advance the approved software scope, ask one concrete question explaining the attempted alternatives. Original question: " + (action.question or action.reason)[:2500]):
                    turn.mark("superseded", {"reason": "Trying autonomous alternatives before interrupting the owner"})
                    return None
                if action.kind == ActionKind.BROWSER:
                    message = "Browser journey verification is required for: " + "; ".join(action.checks) + ". No browser journey runner is installed in this stage; verify these checks or resume with a correction."
                    status = "blocked"
                else:
                    message, status = action.question or action.reason, "applied"
                pause(message, {"decision_id": turn.decision_id, "action": action.kind.value}, decision_status=status,
                      decision_result={"message": message, "evidence": list(action.evidence), "verification": "unverified" if action.kind == ActionKind.BROWSER else None})
                return None
            if normalization:
                ps.event(project["id"], "proposal_normalized", {"decision_id": turn.decision_id, "explanation": normalization})
            turn.mark("validated", {"evidence": list(action.evidence), "normalization": normalization})
            return action, converted, spec
        except (ProjectError, TypeError, ValueError) as exc:
            turn.mark("rejected", {"error": str(exc), "recovery": "Automatic correction uses the remaining decision allowance"})
            ps.event(project["id"], "proposal_rejected", {"reason": str(exc), "decision_id": turn.decision_id,
                                                       "recovery": "Retrying automatically with the exact rejection and valid examples"})
            context["rejected_proposal"] = result
            context["correction_required"] = str(exc)
            context["repair_instruction"] = "Correct the rejected response using action_examples. Do not ask the owner to fix JSON, dependencies or model formatting. Do not add fields, change scope or increase allowances. Use continue_plan for an unchanged next queued task."
            invalid += 1
