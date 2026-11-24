"""Owner-selected unattended recovery. Models cannot authorize or reset its limits."""
from __future__ import annotations

import hashlib
import json
import time

from .spec import fixed
from .store import Projects


def _json(value):
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def instructions(store, pid):
    return _json(store.kv_get("project_auto_context:" + pid))


def ready(store, pid):
    return instructions(store, pid).get("next_at", 0) <= time.time()


def remember_policy(cfg, store, pid):
    store.kv_set("project_automation:" + pid, json.dumps({"enabled": cfg.projects.autonomous,
                 "recovery_rounds": cfg.projects.auto_recovery_rounds}))


def _remaining(cfg, ps, p):
    milestone = ps.milestones(p["id"])[p["current_milestone"]]
    return min(p["budget_usd"] - ps.spent(p["id"]),
               milestone_limit(cfg, p, milestone) - ps.spent(p["id"], p["current_milestone"]))


def milestone_limit(cfg, project, milestone):
    """Unattended delivery may reallocate estimates, never increase the total approved cap."""
    return project["budget_usd"] if cfg.projects.autonomous else milestone["estimate_usd"] * cfg.projects.escalate_budget_factor


def _counter_key(store, p, problem):
    progress = store._one("SELECT COALESCE(MAX(id),0) AS id FROM project_events WHERE project_id=? "
                          "AND kind IN ('approval','merge','spike_finished','reply','spec_approved','milestone_progressed')", (p["id"],))
    value = [fixed(p["spec"]), p["current_milestone"], progress["id"], problem]
    return "project_auto_round:" + p["id"] + ":" + hashlib.sha256(json.dumps(value).encode()).hexdigest()


def _context(store, p, problem, instruction, delay):
    store.kv_set("project_auto_context:" + p["id"], json.dumps({"problem": problem, "milestone": p["current_milestone"],
                 "instruction": instruction[:6000], "next_at": time.time() + delay}))


def schedule_repair(cfg, store, p, problem, instruction):
    """Start another bounded planning round, without inventing an owner reply."""
    if not cfg.projects.autonomous or not p.get("approved_at"):
        return False
    ps = Projects(store)
    with store.transaction():
        current = ps.get(p["id"])
        if current["status"] != "running" or current["spec"] != p["spec"] or _remaining(cfg, ps, current) <= 0:
            return False
        key = _counter_key(store, current, problem)
        count = int(store.kv_get(key) or 0)
        if count >= cfg.projects.auto_recovery_rounds:
            return False
        store.kv_set(key, str(count + 1))
        _context(store, current, problem, instruction, cfg.projects.auto_retry_delay_s)
        ps.update(p["id"], observed_sha=None, last_error=None)
        ps.event(p["id"], "automatic_recovery", {"kind": problem, "round": count + 1, "instruction": instruction[:6000]})
        store.kv_set("project_activity:" + p["id"], "Recovering automatically: " + instruction[:300])
    return True


def recover_allowance(cfg, store, p, task, run):
    """Reallocate approved remaining project funds to the SAME task, at most twice."""
    if not cfg.projects.autonomous or not p.get("approved_at") or not run or run.get("stop_kind") not in ("allowance_exhausted", "stalled", "lost_worker", "task_failure"):
        return False
    ps = Projects(store)
    eid = run.get("execution_id") or task.get("execution_id")
    if not eid:
        return False
    with store.transaction():
        current = ps.get(p["id"])
        if current["status"] not in ("running", "waiting_for_you") or current["spec"] != p["spec"] or task["milestone"] != current["current_milestone"]:
            return False
        persisted_task = store.queue_item(task["id"])
        if not persisted_task or persisted_task.get("project_id") != p["id"] or persisted_task.get("run_id") != run["id"] or persisted_task["status"] != "failed":
            return False
        if store._one("SELECT id FROM queue_items WHERE project_id=? AND status='running'", (p["id"],)):
            return False
        allowance = store._one("SELECT * FROM execution_allowances WHERE id=?", (eid,))
        if not allowance or allowance["cancelled"] or allowance["active_run"]:
            return False
        if run["stop_kind"] == "task_failure" and allowance["deadline"] > time.time() and allowance["spent_usd"] < allowance["budget_usd"]:
            return False
        if store._one("SELECT id FROM execution_calls WHERE execution_id=? AND status='reserved' LIMIT 1", (eid,)):
            return False
        if store._one("SELECT name FROM execution_checkpoints WHERE execution_id=? AND name LIKE 'external:%' AND status IN ('pending','uncertain','conflict')", (eid,)):
            return False
        key = "project_auto_allowance:" + p["id"] + ":" + eid
        record = _json(store.kv_get(key))
        count = int(record.get("count", 0))
        if record.get("last_run_id") == run["id"]:
            return False
        if count >= cfg.projects.auto_recovery_rounds:
            return False
        unspent = max(0, allowance["budget_usd"] - allowance["spent_usd"])
        expired = allowance["deadline"] <= time.time()
        extra = 0 if expired and unspent >= max(.5, cfg.projects.min_build_budget_usd) else max(allowance["budget_usd"], cfg.projects.min_build_budget_usd)
        if unspent + extra > _remaining(cfg, ps, current) or unspent + extra <= 0:
            return False
        deadline = max(allowance["deadline"], time.time() + cfg.limits.max_runtime_s)
        store.conn.execute("UPDATE execution_allowances SET budget_usd=budget_usd+?,deadline=?,max_recoveries=max_recoveries+?,repeat_limit=repeat_limit+? WHERE id=?",
                           (extra, deadline, int(run["stop_kind"] in ("stalled", "lost_worker")), cfg.limits.repeat_action_limit if run["stop_kind"] == "stalled" else 0, eid))
        store.kv_set(key, json.dumps({"count": count + 1, "last_run_id": run["id"]}))
        store.kv_set("project_recovery:" + p["id"], eid)
        store.conn.execute("UPDATE queue_items SET project_processed=1 WHERE project_id=? AND execution_id=? AND status IN ('failed','cancelled')", (p["id"], eid))
        store.conn.execute("UPDATE queue_items SET status='cancelled',project_processed=1 WHERE project_id=? AND status IN ('queued','tentative')", (p["id"],))
        ps.update(p["id"], status="running", observed_sha=None, last_error=None)
        message = ("Replan the stopped task from its retained code, tests and failure diagnosis. Use a different repair approach, "
                   "split work if useful, preserve the original shared task lineage and all costs. Do not turn unavailable "
                   "human/browser observations or report-writing into automated build criteria.")
        _context(store, current, "task_repair", message, cfg.projects.auto_retry_delay_s)
        ps.event(p["id"], "automatic_recovery", {"kind": "task_allowance", "task_id": task["id"], "execution_id": eid, "round": count + 1,
                 "before_usd": allowance["budget_usd"], "after_usd": allowance["budget_usd"] + extra,
                 "spent_usd": allowance["spent_usd"], "deadline": deadline, "reason": run.get("state_reason")})
        from ..runqueue import set_lane_paused
        set_lane_paused(store, "project-" + p["id"], False)
        store.conversation_append(current["conversation_id"], "assistant", f"Recovering automatically: task allowance ${allowance['budget_usd']:g} → ${allowance['budget_usd'] + extra:g}, within the approved ${current['budget_usd']:g} project cap. Previous spending and evidence are retained. I will revise the plan before retrying.")
    return True


def recover_lineage(cfg, store, p, eid):
    task = store._one("SELECT q.* FROM queue_items q JOIN runs r ON r.id=q.run_id WHERE q.project_id=? AND r.execution_id=? "
                      "AND q.status='failed' ORDER BY q.id DESC LIMIT 1", (p["id"], eid))
    return bool(task and recover_allowance(cfg, store, p, task, store.get_run(task["run_id"])))


def recover_environment(cfg, store, p):
    """Retry only a machine-paused environment failure; explicit owner pauses win."""
    from ..runqueue import ENV_PAUSE_NOTE, PAUSED_KEY, lane_paused, set_lane_paused
    name = "project-" + p["id"]
    if not cfg.projects.autonomous or p["status"] != "running" or store.kv_get(PAUSED_KEY) == "1" or not ready(store, p["id"]):
        return
    if not lane_paused(store, name) or store.kv_get("queue_pause_reason:" + name) != "environment":
        return
    task = store._one("SELECT id,note FROM queue_items WHERE project_id=? AND status='queued' ORDER BY position LIMIT 1", (p["id"],))
    if not task or not str(task["note"] or "").startswith(ENV_PAUSE_NOTE):
        return
    ps = Projects(store)
    with store.transaction():
        if ps.get(p["id"])["status"] != "running" or store.kv_get("queue_pause_reason:" + name) != "environment":
            return
        key = "project_auto_environment:" + p["id"] + ":" + str(task["id"])
        count = int(store.kv_get(key) or 0)
        if count >= cfg.projects.auto_recovery_rounds:
            from .service import stop
            stop(ps, p, "The worker environment is still unavailable after automatic rechecks. " + str(task["note"] or "")[:1000], {"task_id": task["id"], "automatic_rechecks": count})
            return
        store.kv_set(key, str(count + 1))
        _context(store, p, "environment", "Rechecking the worker environment before continuing the retained task.", cfg.projects.auto_retry_delay_s)
        set_lane_paused(store, name, False)
        ps.event(p["id"], "automatic_recovery", {"kind": "environment", "task_id": task["id"], "round": count + 1})


def review_checkpoint(cfg, store, p):
    """Verify first, continue a preview with explicit pending checks, review once at the end."""
    if not cfg or not cfg.projects.autonomous:
        return False
    ps = Projects(store)
    with store.transaction():
        current = ps.get(p["id"])
        if current["status"] != "at_checkpoint" or current["spec"] != p["spec"]:
            return False
        index = current["current_milestone"]
        milestones = ps.milestones(p["id"])
        report = _json(milestones[index]["report"])
        checks = report.get("checks", [])
        if not checks or any(c.get("status") == "failed" for c in checks):
            return False
        pending = [c for c in checks if c.get("status") != "passed"]
        verification_key = "project_auto_verify:" + p["id"] + ":" + str(index) + ":" + hashlib.sha256(fixed(current["spec"]).encode()).hexdigest()
        if pending and not store.kv_get(verification_key) and _remaining(cfg, ps, current) >= max(cfg.projects.min_build_budget_usd, cfg.projects.lead_budget_usd):
            store.kv_set(verification_key, "1")
            instruction = ("Verify this milestone before moving on. Reuse merged tests first; add machine-testable evidence for "
                           "the EXACT approved check text where possible. Use fixtures or simulated inputs to build a usable preview "
                           "when real devices or owner observations are unavailable. Preserve those real-world checks as unverified. "
                           "After this verification pass, evaluate the milestone again; do not loop on tests requiring unavailable hardware.")
            _context(store, current, "milestone_verification", instruction, 0)
            ps.update(p["id"], status="running", observed_sha=None, last_error=None)
            ps.event(p["id"], "verification_repair", {"milestone": index, "checks": pending, "instruction": instruction})
            store.conversation_append(current["conversation_id"], "assistant", "I will check the missing milestone evidence automatically before continuing. Physical/device observations will stay unverified.")
            return True
        final = index + 1 == len(milestones)
        previous_pending = [{"milestone": m["idx"], "name": m["name"], "checks": _json(m["report"]).get("checks", [])}
                            for m in milestones if m["status"] == "verification_pending" and m["idx"] < index]
        if final and (pending or previous_pending):
            if report.get("final_review"):
                return False  # the final handoff is a real review, never an endless auto-resume loop
            report.update(final_review=True, pending_milestones=previous_pending)
            store.conn.execute("UPDATE milestones SET report=? WHERE project_id=? AND idx=?", (json.dumps(report), p["id"], index))
            ps.event(p["id"], "preview_ready", {"milestone": index, "pending_milestones": len(previous_pending), "pending_checks": len(pending)})
            store.conversation_append(current["conversation_id"], "assistant", "The project preview is ready for your review. Remaining real-world or unverified checks are listed explicitly; they have not been marked passed.")
            store.kv_set("project_auto_context:" + p["id"], None)
            return True
        status = "verification_pending" if pending else "done"
        store.conn.execute("UPDATE milestones SET status=?,finished_at=? WHERE project_id=? AND idx=?",
                           (status, None if pending else time.time(), p["id"], index))
        if not final:
            store.conn.execute("UPDATE milestones SET status='current',started_at=? WHERE project_id=? AND idx=?", (time.time(), p["id"], index + 1))
        store.conn.execute("UPDATE queue_items SET status='cancelled',project_processed=1 WHERE project_id=? AND milestone=? AND status IN ('queued','tentative')", (p["id"], index))
        ps.update(p["id"], status="done" if final else "running", current_milestone=index if final else index + 1, observed_sha=None, last_error=None)
        ps.event(p["id"], "milestone_progressed", {"milestone": index, "verification": status, "pending_checks": pending})
        store.kv_set("project_auto_context:" + p["id"], None)
        store.conversation_append(current["conversation_id"], "assistant", "Milestone software work is ready. " +
                                 ("Unverified checks remain on the final review list. " if pending else "All milestone checks have passing evidence. ") +
                                 ("Project complete." if final else "Continuing to the next milestone within the approved budget."))
        return True


def recover_waiting(cfg, store, p):
    """Only positively identified internal stops can reopen without an owner response."""
    if not cfg.projects.autonomous or not p.get("approved_at"):
        return False
    if p["status"] == "at_checkpoint":
        return review_checkpoint(cfg, store, p)
    if p["status"] != "waiting_for_you":
        return False
    from .recovery import recovery_state
    state = recovery_state(store, p)
    task = state["decision"].get("task")
    if task:
        run = store.get_run(task["run_id"])
        queue = store.queue_item(task["id"])
        return recover_allowance(cfg, store, p, queue, run)
    escalation = store._one("SELECT detail FROM project_events WHERE project_id=? AND kind='escalation' ORDER BY id DESC LIMIT 1", (p["id"],))
    detail = _json(escalation["detail"]) if escalation else {}
    if "shared allowance is exhausted" in (p.get("last_error") or "") and detail.get("execution_id"):
        return recover_lineage(cfg, store, p, detail["execution_id"])
    latest = store._one("SELECT status,action,proposal FROM project_decisions WHERE project_id=? ORDER BY id DESC LIMIT 1", (p["id"],))
    safe_cancel = "CancelledError" in (p.get("last_error") or "") and latest and latest["action"] == "propose" and not latest["proposal"]
    lost = store._one("SELECT detail FROM project_events WHERE project_id=? AND kind='lead_recovery' ORDER BY id DESC LIMIT 1", (p["id"],))
    safe_lost = "Project lead worker was lost" in (p.get("last_error") or "") and lost and _json(lost["detail"]).get("safe_to_retry") is True
    if state["can_retry"] or safe_cancel or safe_lost:
        ps = Projects(store)
        with store.transaction():
            if ps.get(p["id"])["status"] != "waiting_for_you":
                return False
            ps.update(p["id"], status="running", observed_sha=None, last_error=None)
            if schedule_repair(cfg, store, ps.get(p["id"]), "planning", "Repair the saved internal planning error and continue the approved project; do not ask the owner to repair model formatting or worker restarts."):
                return True
            ps.update(p["id"], status="waiting_for_you", last_error=p.get("last_error"))
    return False
