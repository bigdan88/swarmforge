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


def _verification_key(project):
    return "project_auto_verify:" + project["id"] + ":" + str(project["current_milestone"]) + ":" + hashlib.sha256(fixed(project["spec"]).encode()).hexdigest()


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
                          "AND kind IN ('approval','merge','spike_finished','reply','spec_approved','milestone_progressed','changes_requested')", (p["id"],))
    value = [fixed(p["spec"]), p["current_milestone"], progress["id"], problem]
    return "project_auto_round:" + p["id"] + ":" + hashlib.sha256(json.dumps(value).encode()).hexdigest()


def _context(store, p, problem, instruction, delay):
    store.kv_set("project_auto_context:" + p["id"], json.dumps({"problem": problem, "milestone": p["current_milestone"],
                 "instruction": instruction[:6000], "next_at": time.time() + delay}))


SUPERVISOR_DELAYS_S = (120, 300, 900, 1800, 3600)


def _defer_key(project):
    return "project_defer_failed:" + project["id"] + ":" + str(project["current_milestone"])


def deferral_allowed(store, project):
    """After repeated unattended repair rounds, a milestone may move on with its failed
    checks recorded (never marked passed) on the final review list."""
    return bool(store.kv_get(_defer_key(project)))


def _human_only(store, p):
    """Stops that unattended operation must never override."""
    error = p.get("last_error") or ""
    if error.startswith("Paused by you") or p.get("previous_spec"):
        return "paused or amended by the owner"
    if error.startswith("Project lead worker was lost"):
        lost = store._one("SELECT detail FROM project_events WHERE project_id=? AND kind='lead_recovery' ORDER BY id DESC LIMIT 1", (p["id"],))
        if not (lost and _json(lost["detail"]).get("safe_to_retry") is True):
            return "a lead turn stopped during a repository write"
    # The stopped task's own allowance: cancelled by the owner, still owned, or with a
    # model call in flight. None of these is a stop the supervisor may talk past.
    escalation = store._one("SELECT detail FROM project_events WHERE project_id=? AND kind='escalation' ORDER BY id DESC LIMIT 1", (p["id"],))
    run_id = _json(escalation["detail"]).get("run_id") if escalation else None
    for row in store._all("SELECT a.cancelled,a.active_run,a.id FROM execution_allowances a JOIN runs r ON r.execution_id=a.id "
                          "WHERE r.id=?", (run_id,)) if run_id else []:
        if row["cancelled"] or row["active_run"] or store._one(
                "SELECT id FROM execution_calls WHERE execution_id=? AND status='reserved' LIMIT 1", (row["id"],)):
            return "a stopped task allowance is cancelled or still in use"
    # An external action (PR, push, merge) with an unknown outcome needs a person to look.
    if store._one("SELECT c.name FROM execution_checkpoints c JOIN queue_items q ON q.execution_id=c.execution_id "
                  "WHERE q.project_id=? AND c.name LIKE 'external:%' AND c.status IN ('pending','uncertain','conflict') LIMIT 1",
                  (p["id"],)):
        return "an external action has an uncertain outcome"
    return None


def supervision_pending(cfg, store):
    """True while some autonomous project is stopped and the supervisor may still reopen it
    later (back-off). The queue worker stays alive for these instead of exiting as idle."""
    if not cfg.projects.autonomous or not cfg.projects.supervisor_rounds_per_day:
        return False
    ps = Projects(store)
    now = time.time()
    for p in ps.list():
        if p["status"] not in ("waiting_for_you", "at_checkpoint") or not p.get("approved_at"):
            continue
        if p["status"] == "at_checkpoint" and _json(ps.milestones(p["id"])[p["current_milestone"]]["report"]).get("final_review"):
            continue
        if _human_only(store, p) or _remaining(cfg, ps, p) < max(cfg.projects.lead_budget_usd, .5):
            continue
        rounds = [t for t in _json(store.kv_get("project_supervisor:" + p["id"])).get("rounds", []) if now - t < 86400]
        if len(rounds) < cfg.projects.supervisor_rounds_per_day:
            return True
    return False


def supervise(cfg, store, p):
    """Last-resort unattended recovery: an autonomous project with budget left never just sits.

    Every stop that is not owner-only becomes another bounded, backed-off planning round
    that carries the exact stop reason. After two rounds on one milestone, the milestone may
    move on with unresolved checks listed for the final review instead of halting the whole
    project. Limits are deterministic: rounds per day, the approved project cap, and the
    owner-only stops above. Models cannot reset them."""
    if not cfg.projects.autonomous or not p.get("approved_at") or p["status"] not in ("waiting_for_you", "at_checkpoint"):
        return False
    if p["status"] == "at_checkpoint" and _json(Projects(store).milestones(p["id"])[p["current_milestone"]]["report"]).get("final_review"):
        return False  # the final preview is a real owner review
    if _human_only(store, p) or store._one("SELECT id FROM queue_items WHERE project_id=? AND status='running' LIMIT 1", (p["id"],)):
        return False
    ps = Projects(store)
    key = "project_supervisor:" + p["id"]
    now = time.time()
    with store.transaction():
        current = ps.get(p["id"])
        if current["status"] != p["status"] or current["spec"] != p["spec"]:
            return False
        if _remaining(cfg, ps, current) < max(cfg.projects.lead_budget_usd, .5):
            return False  # money decisions stay with the owner
        state = _json(store.kv_get(key))
        rounds = [t for t in state.get("rounds", []) if now - t < 86400]
        if len(rounds) >= cfg.projects.supervisor_rounds_per_day:
            return False
        if rounds and now - rounds[-1] < SUPERVISOR_DELAYS_S[min(len(rounds) - 1, len(SUPERVISOR_DELAYS_S) - 1)]:
            return False  # back off; the next tick retries
        milestone_rounds = state.get("milestone_rounds", 0) if state.get("milestone") == current["current_milestone"] else 0
        reason = (current.get("last_error") or "the milestone checkpoint has failing checks")[:1500]
        milestone_rounds += 1
        rounds.append(now)
        store.kv_set(key, json.dumps({"rounds": rounds, "milestone": current["current_milestone"],
                                      "milestone_rounds": milestone_rounds, "last_reason": reason}))
        defer = milestone_rounds >= 2
        if defer:
            store.kv_set(_defer_key(current), "1")
        instruction = ("The project stopped while the owner is away: " + reason + "\n"
                       "Unattended rule: do not wait for the owner and do not repeat an approach that already failed or was rejected. "
                       "Choose a different, smaller step: re-plan the blocked work into smaller tasks, use retained code and "
                       "evidence, or pick independent work that is still possible. Keep human-only or browser observations unverified. ")
        if defer:
            instruction += ("This milestone has already had an unattended repair round. If its remaining blocker cannot be fixed "
                            "with a different approach, evaluate the milestone now: failed or unverified checks will be listed "
                            "for the owner's final review (never marked passed) and work continues on the next milestone.")
        _context(store, current, "supervisor", instruction, cfg.projects.auto_retry_delay_s)
        from ..runqueue import set_lane_paused
        set_lane_paused(store, "project-" + p["id"], False)
        store.conn.execute("UPDATE queue_items SET project_processed=1 WHERE project_id=? AND status IN ('failed','cancelled')", (p["id"],))
        ps.update(p["id"], status="running", observed_sha=None, last_error=None)
        ps.event(p["id"], "automatic_recovery", {"kind": "supervisor", "round": len(rounds), "milestone_round": milestone_rounds,
                                                 "deferral_allowed": defer, "reason": reason})
        store.kv_set("project_activity:" + p["id"], "Recovering automatically: " + reason[:300])
        store.conversation_append(current["conversation_id"], "assistant",
                                  f"Continuing unattended (round {len(rounds)} today): {reason[:400]}")
    return True


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
        if not checks or (any(c.get("status") == "failed" for c in checks) and not deferral_allowed(store, current)):
            return False
        pending = [c for c in checks if c.get("status") != "passed"]
        verification_key = _verification_key(current)
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
            store.kv_set("project_recovery:" + p["id"], None)
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
        store.kv_set("project_recovery:" + p["id"], None)
        store.conversation_append(current["conversation_id"], "assistant", "Milestone software work is ready. " +
                                 ("Unverified checks remain on the final review list. " if pending else "All milestone checks have passing evidence. ") +
                                 ("Project complete." if final else "Continuing to the next milestone within the approved budget."))
        return True


def recover_waiting(cfg, store, p):
    """Reopen an unattended project: specific repairs first, then the bounded supervisor."""
    if not cfg.projects.autonomous or not p.get("approved_at"):
        return False
    if p["status"] == "at_checkpoint":
        return review_checkpoint(cfg, store, p) or supervise(cfg, store, p)
    if p["status"] == "waiting_for_you":
        return _recover_known(cfg, store, p) or supervise(cfg, store, Projects(store).get(p["id"]))
    return False


def _recover_known(cfg, store, p):
    """Positively identified internal stops, each with its own specific repair."""
    if p["status"] != "waiting_for_you":
        return False
    from .recovery import recovery_state
    state = recovery_state(store, p)
    task = state["decision"].get("task")
    if task:
        run = store.get_run(task["run_id"])
        queue = store.queue_item(task["id"])
        if recover_allowance(cfg, store, p, queue, run):
            return True
    escalation = store._one("SELECT detail FROM project_events WHERE project_id=? AND kind='escalation' ORDER BY id DESC LIMIT 1", (p["id"],))
    detail = _json(escalation["detail"]) if escalation else {}
    if "shared allowance is exhausted" in (p.get("last_error") or "") and detail.get("execution_id"):
        return recover_lineage(cfg, store, p, detail["execution_id"])
    latest = store._one("SELECT status,action,proposal FROM project_decisions WHERE project_id=? ORDER BY id DESC LIMIT 1", (p["id"],))
    # Older versions demanded a fresh merge in each milestone, even when earlier
    # project work already built its features. Recover that known browser handoff
    # without claiming a browser pass or repeating its exhausted verification work.
    if (latest and latest["status"] == "blocked" and latest["action"] == "request_browser_verification"
            and not (p.get("last_error") or "").startswith("Paused by you") and not p.get("previous_spec")):
        from .service import verify_checks
        ps = Projects(store)
        if (not store._one("SELECT id FROM queue_items WHERE project_id=? AND status='running' LIMIT 1", (p["id"],))
                and any(q.get("merged_sha") and q["milestone"] <= p["current_milestone"] for q in ps.items(p["id"]))
                and not any(c["status"] == "failed" for c in verify_checks(store, p))):
            with store.transaction():
                current = ps.get(p["id"])
                if current["status"] != "waiting_for_you" or current["spec"] != p["spec"]:
                    return False
                ps.update(p["id"], status="running", observed_sha=None, last_error=None)
                if schedule_repair(cfg, store, ps.get(p["id"]), "verification_handoff", "Evaluate the existing merged project work against this milestone. Do not require a fresh merge or another evidence-only build. Keep unavailable browser observations unverified for the final preview review."):
                    store.kv_set(_verification_key(current), "1")
                    return True
                ps.update(p["id"], status="waiting_for_you", last_error=p.get("last_error"))
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
