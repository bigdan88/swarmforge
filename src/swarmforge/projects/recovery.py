"""Read-only recovery explanations, shared by the dashboard and guarded retry action."""
from __future__ import annotations

import json
import hashlib


def _object(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def recovery_state(store, project):
    rows = store._all("SELECT id,epoch,created_at,action,status,reason,result FROM project_decisions "
                      "WHERE project_id=? ORDER BY id DESC LIMIT 12", (project["id"],))
    latest = rows[0] if rows else None
    rejected = []
    for row in rows:
        if row["status"] != "rejected":
            continue
        error = str(_object(row["result"]).get("error") or "The proposed action failed validation.")[:4000]
        explanation = ("The response used fields in the wrong part of an action." if "fields outside" in error else
                       "The task order depended on work that was not eligible or complete." if "Dependencies" in error else
                       "The response needed a complete task definition or an existing task ID." if "task object" in error else
                       "The response was not valid action JSON." if "JSON" in error or "decoded" in error else
                       "The proposed action did not pass the project's validation checks.")
        rejected.append({"id": row["id"], "ts": row["created_at"], "action": row["action"], "explanation": explanation,
                         "error": error, "reason": str(row["reason"] or "")[:1500]})
        resolved = store._one("SELECT d.id,d.created_at FROM project_decisions d WHERE d.project_id=? AND d.id>? AND d.status='applied' "
                              "AND d.action IN ('plan','continue_plan','investigate','request_review','evaluate_milestone','checkpoint') "
                              "AND NOT EXISTS (SELECT 1 FROM project_events e WHERE e.project_id=d.project_id AND e.kind='reply' AND e.ts>? AND e.ts<d.created_at) ORDER BY d.id LIMIT 1",
                              (project["id"], row["id"], row["created_at"]))
        rejected[-1].update(resolved=bool(resolved), resolved_by=resolved["id"] if resolved else None)
        if len(rejected) == 6:
            break
    error = project.get("last_error") or ""
    repairable = any(marker in error for marker in ("invalid proposals", "Automatic planning repair", "decision allowance"))
    can_retry = bool(project.get("approved_at") and project["status"] == "waiting_for_you" and repairable and latest and
                     latest["status"] in ("rejected", "failed", "applied"))
    question = None
    if latest and latest["action"] == "ask_user" and project["status"] == "waiting_for_you":
        question = str(_object(latest["result"]).get("message") or latest["reason"] or "")[:3000]
    running = store._one("SELECT id FROM queue_items WHERE project_id=? AND status='running' LIMIT 1", (project["id"],))
    activity = store._one("SELECT value FROM kv WHERE key=?", ("project_activity:" + project["id"],))
    result = {"can_retry": can_retry, "can_amend": project["status"] in ("waiting_for_you", "at_checkpoint") and not running,
            "latest_decision_id": latest["id"] if latest else None,
            "attempts": sum(r["epoch"] == latest["epoch"] for r in rows) if latest else 0,
            "rejections": rejected, "question": question, "question_reason": str(latest["reason"] or "")[:1500] if question else None,
            "activity": str(activity["value"] or "")[:1000] if activity else None}
    automation = store._one("SELECT value FROM kv WHERE key=?", ("project_automation:" + project["id"],))
    result["automation"] = _object(automation["value"]) if automation else {}
    result.update(_decision(store, project, running, can_retry, question))
    return result


def _decision(store, project, running, planning_retry, question):
    """Explain the current stop, rather than asking for an unspecified approval."""
    event = store._one("SELECT id,kind,detail FROM project_events WHERE project_id=? ORDER BY id DESC LIMIT 1", (project["id"],))
    escalation = store._one("SELECT id,detail FROM project_events WHERE project_id=? AND kind='escalation' ORDER BY id DESC LIMIT 1", (project["id"],))
    detail = _object(escalation["detail"]) if escalation else {}
    error = str(project.get("last_error") or "")
    task = store._one("SELECT q.id,q.title,q.run_id,q.execution_id,q.milestone,r.stop_kind,r.state_reason "
                      "FROM queue_items q JOIN runs r ON r.id=q.run_id WHERE q.project_id=? AND q.run_id=?",
                      (project["id"], detail.get("run_id"))) if detail.get("run_id") else None
    allowance = store._one("SELECT id,budget_usd,spent_usd,deadline,cancelled,active_run FROM execution_allowances WHERE id=?",
                           (task["execution_id"],)) if task and task.get("execution_id") else None
    uncertain = store._one("SELECT name FROM execution_checkpoints WHERE execution_id=? AND name LIKE 'external:%' "
                           "AND status IN ('pending','uncertain','conflict') LIMIT 1", (allowance["id"],)) if allowance else None
    token = hashlib.sha256(json.dumps([project.get("spec"), project["status"], project["current_milestone"], error,
                                      event, task, allowance], sort_keys=True).encode()).hexdigest()
    decision = {"kind": "question" if question else "stopped", "title": "The project needs a correction",
                "summary": question or error or "Review the saved project history before continuing.", "checks": []}
    if planning_retry:
        decision.update(kind="planning", title="The lead could not format a valid next action")
    elif project["status"] == "at_checkpoint":
        milestone = store._one("SELECT name,report FROM milestones WHERE project_id=? AND idx=?", (project["id"], project["current_milestone"]))
        report = _object(milestone["report"]) if milestone else {}
        checks = []
        source = list(report.get("checks", []))
        for pending in report.get("pending_milestones", []):
            source.extend({**check, "text": f"{pending.get('name', 'Earlier milestone')}: {check.get('text', '')}"}
                          for check in pending.get("checks", []) if check.get("status") != "passed")
        for check in source[:30]:
            if not isinstance(check, dict):
                continue
            state = check.get("status")
            checks.append({"text": str(check.get("text") or "")[:4000], "status": state,
                           "explanation": check.get("explanation") or ("Recorded evidence passed." if state == "passed" else
                           "No passing automated evidence is linked to this milestone check. It has not been verified."),
                           "evidence": check.get("evidence", [])[:4]})
        decision.update(kind="checkpoint", title=f"Review milestone {project['current_milestone'] + 1}: {milestone['name'] if milestone else ''}",
                        summary="The lead submitted a milestone for review. This is a checkpoint, not a task crash.",
                        lead_summary=str(report.get("summary") or "")[:6000], checks=checks)
        if report.get("final_review"):
            decision.update(title="Your project preview is ready for review", final_review=True,
                            summary="The swarm continued building while keeping unverified checks on this final review list. Review the preview, request refinements, or accept after verifying the listed checks.")
    elif error.startswith("Paused by you"):
        decision.update(kind="paused", title="You paused this project", summary=error)
    elif task:
        decision.update(kind="task", title="A task stopped before it could finish", task=task,
                        summary=str(task["state_reason"] or error)[:3000])
        if allowance:
            decision["allowance"] = {k: allowance[k] for k in ("budget_usd", "spent_usd", "deadline")}
            # A preflight budget stop has no checks. Show the latest actual attempt's
            # evidence in the same lineage instead of implying all tests failed.
            attempt = store._one("SELECT r.id FROM runs r WHERE r.execution_id=? AND EXISTS "
                                 "(SELECT 1 FROM acceptance a WHERE a.run_id=r.id) ORDER BY r.created_at DESC LIMIT 1", (allowance["id"],))
            if attempt:
                checks = store._all("SELECT text,status,evidence FROM acceptance WHERE run_id=? AND cycle="
                                    "(SELECT MAX(cycle) FROM acceptance WHERE run_id=?) ORDER BY id LIMIT 50", (attempt["id"], attempt["id"]))
                decision["passed_checks"] = sum(c["status"] in ("pass", "passed", "satisfied") for c in checks)
                decision["checks"] = [{"text": str(c["text"] or "")[:4000], "status": c["status"],
                                       "explanation": str(c["evidence"] or "")[:2000]} for c in checks
                                      if c["status"] not in ("pass", "passed", "satisfied")][:10]
            if task["stop_kind"] == "allowance_exhausted":
                decision.update(title="The task reached its spending or time limit",
                                impact="An ordinary retry keeps this task's original allowance and can stop immediately again. Approve a bounded extension to let the lead revise the plan and repair it.")
    can_extend = bool(project["status"] == "waiting_for_you" and not running and task and allowance
                      and decision["kind"] == "task"
                      and task["milestone"] == project["current_milestone"] and task["stop_kind"] == "allowance_exhausted"
                      and not allowance["cancelled"] and not allowance["active_run"] and not uncertain)
    return {"decision": decision, "decision_token": token, "can_extend_task": can_extend}
