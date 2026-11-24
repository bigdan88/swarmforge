"""Durable project state machine. Only this layer applies lead proposals."""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from . import lead
from .autonomy import milestone_limit
from .repository import Repository
from .spec import ProjectError, fixed, replace_living, validate
from .store import Projects


def lane(pid):
    return "project-" + pid


def approve(store, pid, *, expected_spec=None):
    ps = Projects(store)
    with store.transaction():
        p = ps.get(pid)
        if p["status"] != "drafting":
            raise ProjectError("This project has already been launched")
        if expected_spec is not None:
            import hashlib
            if expected_spec != hashlib.sha256(p["spec"].encode()).hexdigest():
                raise ProjectError("The spec changed. Reload and review before launching.")
        validate(p["definition"])
        if any(x["id"] != pid and x["repo"].lower() == p["repo"].lower() and x["status"] not in ("drafting", "done", "cancelled") for x in ps.list()):
            raise ProjectError("Another active project owns this repository; finish or cancel it first")
        now = time.time()
        for i, m in enumerate(p["definition"]["milestones"]):
            store.conn.execute("INSERT INTO milestones(project_id,idx,name,estimate_usd,estimate_hours,status,started_at) VALUES (?,?,?,?,?,?,?)",
                               (pid, i, m["name"], m["estimate_usd"], m["estimate_hours"], "current" if i == 0 else "ahead", now if i == 0 else None))
        store.conn.execute("UPDATE projects SET status='running',approved_at=? WHERE id=?", (now, pid))
        store.conn.execute("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)", (pid, now, "approval", json.dumps({"spec": p["spec"]})))


def pause(store, pid):
    ps = Projects(store)
    with store.transaction():  # check and change together: no second action slips in between
        p = ps.get(pid)
        if p["status"] != "running":
            raise ProjectError("Only a running project can be paused")
        ps.update(pid, status="waiting_for_you", last_error="Paused by you. Running work will finish; no new task will start.")
        ps.event(pid, "pause", {})


def cancel(store, pid):
    ps = Projects(store)
    with store.transaction():  # all queued work, running cancellations and the status change, or none
        ps.get(pid)
        for q in ps.items(pid):
            if q["status"] in ("queued", "tentative"):
                store.queue_update(q["id"], status="cancelled")
            elif q["status"] == "running" and q.get("run_id"):
                store.request_cancel(q["run_id"])
        ps.update(pid, status="cancelled", last_error=None)
        ps.event(pid, "cancel", {})


def amend(store, pid, definition):
    """Explicit human approval of a new fixed spec; the lead cannot call this operation."""
    from .spec import render
    ps = Projects(store)
    with store.transaction():  # every check below holds when the new spec is written
        _amend(store, ps, pid, definition, render)


def _amend(store, ps, pid, definition, render):
    p = ps.get(pid)
    if p["status"] not in ("waiting_for_you", "at_checkpoint"):
        raise ProjectError("Pause at a checkpoint before approving a revised fixed spec")
    d = validate(definition)
    if d["repo"].lower() != p["repo"].lower() or d["repository_kind"] != p["definition"]["repository_kind"]:
        raise ProjectError("Repository migration needs a separate project; this approval edits the current project's scope")
    if len(d["milestones"]) != len(p["definition"]["milestones"]) or d["milestones"][:p["current_milestone"]] != p["definition"]["milestones"][:p["current_milestone"]]:
        raise ProjectError("Preserve the milestone count and completed milestones")
    if any(q["status"] == "running" for q in ps.items(pid)):
        raise ProjectError("Wait for running work to finish before revising the fixed spec")
    living = p["spec"][len(fixed(p["spec"])) + len("## Living\n"):]
    store.conn.execute("UPDATE projects SET name=?,budget_usd=?,definition=?,spec=?,previous_spec=?,observed_sha=NULL WHERE id=?",
                       (d["name"], d["budget_usd"], json.dumps(d), render(d, living), p.get("previous_spec") or p["spec"], pid))
    for i, m in enumerate(d["milestones"]):
        if i >= p["current_milestone"]:
            store.conn.execute("UPDATE milestones SET name=?,estimate_usd=?,estimate_hours=?,report=NULL,status=? WHERE project_id=? AND idx=?",
                               (m["name"], m["estimate_usd"], m["estimate_hours"], "current" if i == p["current_milestone"] else "ahead", pid, i))
    store.conn.execute("UPDATE projects SET status='waiting_for_you' WHERE id=?", (pid,))
    store.conn.execute("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)", (pid, time.time(), "spec_approved", json.dumps({"before": p["spec"], "after": render(d, living)})))


def resume(store, pid, reply="continue", *, confirm_manual=False, expected_spec=None, expected_recovery=None):
    ps = Projects(store)
    if not reply.strip():
        raise ProjectError("Add a reply or correction")
    from ..runqueue import set_lane_paused
    with store.transaction():
        p = ps.get(pid)
        if expected_spec is not None:
            import hashlib
            if expected_spec != hashlib.sha256(p["spec"].encode()).hexdigest():
                raise ProjectError("The approved spec changed. Reload before continuing.")
        if p["status"] not in ("at_checkpoint", "waiting_for_you"):
            raise ProjectError("Project is not waiting for a reply")
        if expected_recovery is not None or p["status"] == "waiting_for_you":
            from .recovery import recovery_state
            recovery = recovery_state(store, p)
            if expected_recovery is not None and expected_recovery != recovery["decision_token"]:
                raise ProjectError("This decision changed. Reload and review the current stop before continuing.")
            task = recovery["decision"].get("task")
            if task and task.get("stop_kind") == "allowance_exhausted" and store.kv_get("project_recovery:" + pid) != task.get("execution_id"):
                raise ProjectError("This task needs a bounded allowance extension before replanning. Use the project's recovery controls to approve the additional amount and time window.")
        status, index = "running", p["current_milestone"]
        now = time.time()
        if p["status"] == "at_checkpoint" and reply.strip().lower() in ("continue", "approved", "looks good"):
            milestones = ps.milestones(pid)
            report = json.loads(milestones[index]["report"])
            outstanding = report["checks"] + [check for milestone in report.get("pending_milestones", []) for check in milestone.get("checks", [])]
            if any(c["status"] != "passed" for c in outstanding) and not confirm_manual:
                raise ProjectError("Confirm the checks marked needs you before continuing")
            if any(c["status"] == "failed" for c in outstanding):
                raise ProjectError("Failed milestone checks require repairs before acceptance")
            if report.get("final_review"):
                store.conn.execute("UPDATE milestones SET status='done',finished_at=? WHERE project_id=? AND status='verification_pending'", (now, pid))
            store.conn.execute("UPDATE milestones SET status='done',finished_at=? WHERE project_id=? AND idx=?", (now, pid, index))
            if index + 1 == len(milestones):
                status = "done"
            else:
                index += 1
                store.conn.execute("UPDATE milestones SET status='current',started_at=? WHERE project_id=? AND idx=?", (now, pid, index))
        # Corrections retain the current milestone, and are committed with the state transition.
        store.conn.execute("UPDATE projects SET status=?,current_milestone=?,observed_sha=NULL,last_error=NULL WHERE id=?", (status, index, pid))
        store.conn.execute("INSERT INTO chat_messages(conversation_id,role,content,created_at) VALUES (?,'user',?,?)", (p["conversation_id"], reply, now))
        store.conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, p["conversation_id"]))
        store.conn.execute("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)", (pid, now, "completed" if status == "done" else "reply", json.dumps({"reply": reply, "confirmed_manual": confirm_manual})))
        set_lane_paused(store, lane(pid), False)  # the lane reopens with the reply, not in a later commit
        store.kv_set("project_auto_context:" + pid, None)


def extend_task_allowance(cfg, store, pid, *, expected_spec, expected_recovery, extra_budget, hours):
    """Explicit, bounded recovery approval. Keep the original lineage, costs and evidence."""
    import hashlib
    import math
    from .recovery import recovery_state
    from .decisions import lead_active
    ps = Projects(store)
    if type(extra_budget) not in (int, float) or not math.isfinite(extra_budget) or not .5 <= extra_budget <= 500:
        raise ProjectError("Choose an additional task allowance between $0.50 and $500")
    if type(hours) not in (int, float) or not math.isfinite(hours) or not .05 <= hours <= 72:
        raise ProjectError("Choose a recovery time window between 0.05 and 72 hours")
    with store.transaction():
        p = ps.get(pid)
        recovery = recovery_state(store, p)
        if expected_spec != hashlib.sha256(p["spec"].encode()).hexdigest() or expected_recovery != recovery["decision_token"]:
            raise ProjectError("This decision changed. Reload before extending its allowance.")
        if not recovery["can_extend_task"] or lead_active(store.conn, pid):
            raise ProjectError("This stop cannot use an allowance extension. Inspect its existing recovery path.")
        task = recovery["decision"]["task"]
        allowance = store._one("SELECT * FROM execution_allowances WHERE id=?", (task["execution_id"],))
        if store._one("SELECT id FROM execution_calls WHERE execution_id=? AND status='reserved' LIMIT 1", (allowance["id"],)):
            raise ProjectError("Resolve the outstanding model request before extending its allowance")
        ms = ps.milestones(pid)[p["current_milestone"]]
        remaining = min(p["budget_usd"] - ps.spent(pid), milestone_limit(cfg, p, ms) - ps.spent(pid, p["current_milestone"]))
        unspent = max(0, allowance["budget_usd"] - allowance["spent_usd"])
        if unspent + extra_budget > remaining:
            raise ProjectError(f"This extension exceeds the remaining project or milestone allowance (${max(0, remaining):.2f}). Revise the specification or choose a smaller extension.")
        before = {k: allowance[k] for k in ("budget_usd", "spent_usd", "deadline")}
        deadline = time.time() + hours * 3600
        store.conn.execute("UPDATE execution_allowances SET budget_usd=budget_usd+?,deadline=MAX(deadline,?) WHERE id=?",
                           (extra_budget, deadline, allowance["id"]))
        # Carry the approved lineage through the next lead turn, even though the
        # exhausted attempt is already processed and has no ready replacement.
        store.kv_set("project_recovery:" + pid, allowance["id"])
        store.conn.execute("UPDATE queue_items SET status='cancelled',project_processed=1 WHERE project_id=? AND status IN ('queued','tentative')", (pid,))
        reply = ("Replan and repair the stopped task within the approved scope and extended shared allowance. "
                 "Inspect retained code and test evidence. Keep the same task lineage. "
                 "Separate report instructions and unavailable human/browser checks from machine-testable build acceptance criteria; "
                 "do not retry an unchanged plan that already failed twice. Ask me only for facts or permissions you cannot resolve.")
        resume(store, pid, reply, expected_spec=expected_spec)
        ps.event(pid, "task_allowance_extended", {"task_id": task["id"], "execution_id": allowance["id"], "before": before,
                                                  "additional_usd": extra_budget, "window_hours": hours, "deadline": max(allowance["deadline"], deadline)})


def retry_planning(store, pid, *, expected_spec, expected_decision, correction=""):
    """A human-requested new planning turn; never replay an uncertain repository write."""
    from .recovery import recovery_state
    from .decisions import lead_active
    from ..runqueue import set_lane_paused
    import hashlib
    ps = Projects(store)
    with store.transaction():
        project = ps.get(pid)
        recovery = recovery_state(store, project)
        if expected_spec != hashlib.sha256(project["spec"].encode()).hexdigest() or expected_decision != recovery["latest_decision_id"]:
            raise ProjectError("The project or lead decision changed. Reload before retrying.")
        if not recovery["can_retry"] or lead_active(store.conn, pid):
            raise ProjectError("This stop needs its existing decision or recovery path; an uncertain action cannot be retried as a planning error")
        if store._one("SELECT id FROM queue_items WHERE project_id=? AND status='running'", (pid,)):
            raise ProjectError("Wait for running work before retrying planning")
        reply = "Retry automatic planning within the approved specification and remaining allowance. Repair response formatting internally; continue an eligible existing task instead of recreating it."
        if correction.strip():
            reply += "\nOwner's clarification: " + correction.strip()
        # The ordinary reply creates a new bounded decision epoch; all previous
        # calls remain charged to the same project and milestone.
        resume(store, pid, reply, expected_spec=expected_spec)
        ps.event(pid, "planning_retry", {"decision_id": expected_decision, "correction": correction.strip(), "reason": project["last_error"]})


def github_repo_name(repo):
    from ..repos import normalize_repo
    try:
        return (normalize_repo(repo).full_name or "").lower()
    except ValueError:
        return ""


def can_start(store, item):
    if not item.get("project_id"):
        # Single tasks in a project's repository wait; other repositories are unaffected.
        name = github_repo_name(item["repo"])
        return not name or not any(p["repo"].lower() == name and p["status"] not in ("drafting", "done", "cancelled") for p in Projects(store).list())
    ps = Projects(store)
    p = ps.get(item["project_id"])
    if p["status"] != "running" or not p["observed_sha"] or item["milestone"] != p["current_milestone"]:
        return False
    from .autonomy import ready
    if not ready(store, p["id"]):
        return False
    from .decisions import lead_active
    with store._lock:
        if lead_active(store.conn, p["id"]):
            return False
    queued = [q for q in ps.items(p["id"]) if q["status"] == "queued" and q["milestone"] == p["current_milestone"]]
    if item.get("queue") != lane(p["id"]) or not queued or queued[0]["id"] != item["id"]:
        return False
    dependencies = json.loads(item.get("depends_on") or "[]")
    for key in dependencies:
        matches = [q for q in ps.items(p["id"]) if q["milestone"] == item["milestone"] and q["project_key"] == key]
        if not matches or not (matches[-1].get("merged_sha") or matches[-1]["status"] == "done" and matches[-1]["mode"] == "single" and matches[-1]["project_processed"]):
            return False
    return not any(q["id"] != item["id"] and (q["status"] == "running" or (q["status"] in ("done", "failed", "cancelled") and not q["project_processed"])) for q in ps.items(p["id"]))


def validate_plan(p, result, horizon):
    action = result.get("action")
    if action not in ("plan", "checkpoint", "escalate") or not isinstance(result.get("reason"), str) or not result["reason"].strip():
        raise ProjectError("Lead proposal requires a known action and reason")
    living = result.get("living")
    if isinstance(living, str):
        # Some models include the section heading even when asked for its body.
        living = re.sub(r"\A\s*## Living[ \t]*\r?\n(?:[ \t]*\r?\n)*", "", living, count=1)
    if not isinstance(living, str) or any("### " + h not in living for h in ("Current state", "Decisions made", "Plan", "Plan changes", "Open questions")):
        raise ProjectError("Lead must supply all living sections")
    fixed_text = fixed(p["spec"])
    replace_living(p["spec"], fixed_text + "## Living\n\n" + living)
    for heading in ("Decisions made", "Plan changes"):
        pattern = rf"(?ms)^### {heading}\r?\n(.*?)(?=^### |\Z)"
        old = re.search(pattern, p["spec"][len(fixed_text):])
        new = re.search(pattern, living)
        if not new:
            raise ProjectError(f"Lead must supply the {heading.lower()} section")
        if old and old.group(1).strip() and old.group(1).strip() not in new.group(1):
            living = living[:new.start(1)] + old.group(1).rstrip() + "\n\n" + living[new.start(1):]
    spec = fixed_text + "## Living\n\n" + living
    if action == "escalate":
        attempts = result.get("attempts", [])
        steps = {a.get("step") for a in attempts if isinstance(a, dict) and a.get("step") in (1, 2, 3, 4) and str(a.get("result", "")).strip()}
        if len(steps) < 2 or not isinstance(result.get("proposal"), str) or not result["proposal"].strip():
            raise ProjectError("Escalation rejected: try at least two distinct ladder steps and give a concrete proposal")
    if action == "plan":
        tasks = result.get("tasks")
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 12 or not 1 <= sum(t.get("ready") is True for t in tasks) <= horizon:
            raise ProjectError("Plan requires a short ready horizon and at most 12 tasks")
        keys = set()
        for t in tasks:
            if not isinstance(t.get("ready"), bool):
                raise ProjectError("Task readiness must be a boolean")
            if not isinstance(t.get("key"), str) or not t["key"].strip() or t["key"] in keys:
                raise ProjectError("Task keys must be unique and stable")
            keys.add(t["key"])
            if not isinstance(t.get("title"), str) or not t["title"].strip() or not isinstance(t.get("description"), str):
                raise ProjectError("Tasks need titles and descriptions")
            if t.get("kind") not in ("build", "spike"):
                raise ProjectError("Task kind must be build or spike")
            if not isinstance(t.get("budget_usd"), (float, int)) or not 0 < t["budget_usd"] <= p["budget_usd"]:
                raise ProjectError("Task budget must fit the project budget")
            if t["kind"] == "spike" and t["budget_usd"] > min(2.0, p["budget_usd"] * .1):
                raise ProjectError("A spike budget must be at most $2 or 10% of the project budget, whichever is smaller")
            if t.get("ready") and (not t.get("acceptance_criteria") or not t.get("files") or not all(isinstance(c, str) and c.strip() for c in t["acceptance_criteria"] + t["files"])):
                raise ProjectError("Ready tasks need acceptance criteria and expected files")
    return spec


def verify_checks(store, p):
    ps = Projects(store)
    items = ps.items(p["id"])
    merged = [q for q in items if q["milestone"] == p["current_milestone"] and q["merged_sha"] and q.get("run_id")]
    latest = merged[-1:]  # Earlier evidence may have been invalidated by a subsequent task.
    checks = []
    for text in p["definition"]["milestones"][p["current_milestone"]]["checks"]:
        evidence = []
        status = "needs you"
        for q in latest:
            tests = [t for t in store.test_runs(q["run_id"]) if t["label"] != "baseline"]
            if not tests or tests[-1]["exit_code"] != 0 or tests[-1]["timed_out"]:
                continue
            for a in store._all("SELECT * FROM acceptance WHERE run_id=? AND text=? ORDER BY cycle DESC LIMIT 1", (q["run_id"], text)):
                if a["status"].lower() in ("pass", "passed", "satisfied") and a.get("evidence"):
                    evidence.append({"run": q["run_id"], "sha": q["merged_sha"], "evidence": a["evidence"], "test": tests[-1]["command"]})
                elif a["status"].lower() in ("fail", "failed", "unsatisfied", "not_satisfied"):
                    status = "failed"
        checks.append({"text": text, "status": "passed" if evidence else status, "evidence": evidence})
    return checks


def checkpoint(store, p, reason, *, config=None):
    ps = Projects(store)
    items = ps.items(p["id"])
    checks = verify_checks(store, p)
    if any(c["status"] == "failed" for c in checks):
        raise ProjectError("Milestone has failed checks; the lead must plan repairs")
    ms = ps.milestones(p["id"])[p["current_milestone"]]
    shots = []
    for q in items:
        run = store.get_run(q["run_id"]) if q.get("run_id") else None
        if run and run.get("run_dir") and q["milestone"] == p["current_milestone"]:
            root = Path(run["run_dir"])
            shots.extend(str(x) for x in root.glob("**/*.png") if any(t in x.name.lower() for t in ("light", "dark")))
    report = {"summary": reason, "checks": checks, "screenshots": shots[-8:], "spent_usd": ps.spent(p["id"], p["current_milestone"]),
              "estimate_usd": ms["estimate_usd"], "elapsed_hours": (time.time() - (ms["started_at"] or time.time())) / 3600,
              "estimate_hours": ms["estimate_hours"], "plan_changes": p["spec"].split("## Living", 1)[-1],
              "next_milestone": (p["definition"]["milestones"][p["current_milestone"] + 1] if p["current_milestone"] + 1 < len(p["definition"]["milestones"]) else "Project complete after your confirmation")}
    with store.transaction():
        current = ps.get(p["id"])
        if current["status"] != "running" or current["spec"] != p["spec"] or current["current_milestone"] != p["current_milestone"]:
            return
        store._exec("UPDATE milestones SET status='checkpoint',report=?,spent_usd=? WHERE project_id=? AND idx=?",
                    (json.dumps(report), report["spent_usd"], p["id"], p["current_milestone"]))
        ps.update(p["id"], status="at_checkpoint")
        ps.event(p["id"], "checkpoint", report)
    from .autonomy import review_checkpoint
    if review_checkpoint(config, store, ps.get(p["id"])):
        return
    # An explicitly unconstrained project can advance on real evidence. Missing
    # observations still pause; no agent is allowed to confirm them for the owner.
    automatic = bool(checks) and all(c["status"] == "passed" for c in checks) and p["definition"].get("ask_before", "").strip().lower() == "none"
    if automatic:
        index = p["current_milestone"]
        final = index + 1 == len(p["definition"]["milestones"])
        with store.transaction():
            current = ps.get(p["id"])
            if current["status"] != "at_checkpoint" or current["spec"] != p["spec"] or current["current_milestone"] != index:
                return
            store.conn.execute("UPDATE milestones SET status='done',finished_at=? WHERE project_id=? AND idx=?", (time.time(), p["id"], index))
            if not final:
                store.conn.execute("UPDATE milestones SET status='current',started_at=? WHERE project_id=? AND idx=?", (time.time(), p["id"], index + 1))
            ps.update(p["id"], status="done" if final else "running", current_milestone=index if final else index + 1, observed_sha=None, last_error=None)
            ps.event(p["id"], "milestone_auto_accepted", {"milestone": index, "reason": "Every approved check has passing recorded evidence; no additional approval was requested in the specification."})
        store.conversation_append(p["conversation_id"], "assistant", "Milestone verified automatically\n\n" + reason + "\n\nEvery approved check has passing recorded evidence. " + ("Project complete." if final else "Continuing to the next milestone within the approved allowance."))
    else:
        store.conversation_append(p["conversation_id"], "assistant", "Milestone checkpoint\n\n" + reason + "\n\n" + "\n".join(f"- {c['status']}: {c['text']}" for c in checks) + "\n\nReview the report, request automatic verification, or accept after verifying the listed checks.")


async def advance(cfg, store, pid, *, repository=None, ask=None, transport=None):
    from .decisions import LeadTurn
    turn = LeadTurn.claim(store, pid)
    if turn is None:
        return
    failed = ""
    try:
        with turn.keep_alive():
            return await _advance(cfg, store, pid, repository=repository, ask=ask, transport=transport, turn=turn)
    except BaseException as exc:
        phase = store._one("SELECT status FROM project_decisions WHERE id=?", (turn.decision_id,)) if turn.decision_id else None
        failed = f"{type(exc).__name__}: {exc}"
        turn.mark("failed", {"error": f"{type(exc).__name__}: {exc}"})
        ps = Projects(store)
        current = ps.get(pid)
        if current["status"] == "running":
            from .autonomy import schedule_repair
            from httpx import HTTPStatusError, TransportError
            safe_interruption = isinstance(exc, (asyncio.CancelledError, TimeoutError)) and (not phase or phase["status"] != "executing")
            transient_request = (isinstance(exc, TransportError) or isinstance(exc, HTTPStatusError) and exc.response.status_code in (429, 500, 502, 503, 504)) and (not phase or phase["status"] != "executing")
            stale_snapshot = isinstance(exc, ProjectError) and "Main changed while planning" in str(exc)
            if (safe_interruption or transient_request or stale_snapshot) and schedule_repair(cfg, store, current, "planning", "The planning turn was interrupted before any repository write. Inspect the latest repository and continue from saved evidence."):
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
            stop(ps, current, f"Project lead stopped: {type(exc).__name__}: {exc}. Review the saved decision before resuming.",
                 {"decision_id": turn.decision_id, "error": str(exc)})
        raise
    finally:
        turn.mark("superseded", {"reason": "A current-state guard prevented applying this proposal"})
        turn.release(failed=bool(failed), reason=failed[:1000])


async def _advance(cfg, store, pid, *, repository=None, ask=None, transport=None, turn):
    ps = Projects(store)
    p = ps.get(pid)
    if p["status"] != "running":
        return
    from .autonomy import ready
    if not ready(store, pid):
        return
    activity_key = f"project_activity:{pid}"
    for q in store.queue_items(include_finished=False):
        if (q["status"] == "running" and not q.get("project_id")
                and github_repo_name(q["repo"]) == p["repo"].lower()):
            store.kv_set(activity_key, f"Waiting for task #{q['id']} to finish on this repository")
            return
    own = repository is None
    repo = repository or Repository(cfg, p["repo"])
    ask = ask or lead.call
    try:
        items = ps.items(pid)
        if any(q["status"] == "running" for q in items):
            return
        progress = False
        recovery_lineages = {q["execution_id"] for q in items
                             if q["status"] in ("queued", "tentative") and q.get("execution_id")}
        approved_recovery = store.kv_get("project_recovery:" + pid)
        if approved_recovery:
            recovery_lineages.add(approved_recovery)
        for q in items:
            if q["project_processed"] or q["status"] not in ("done", "failed", "cancelled"):
                continue
            run = store.get_run(q["run_id"]) if q.get("run_id") else None
            if q["status"] == "done":
                run = store.get_run(q["run_id"]) if q.get("run_id") else None
                if q["mode"] == "single" and q["project_key"].startswith("spike:"):
                    sha = None
                    ps.event(pid, "spike", {"task": q["id"], "run": q["run_id"], "report": (run or {}).get("state_reason")})
                elif run and run.get("pr_url"):
                    pr = await asyncio.to_thread(repo.pull, run["pr_url"])
                    if not pr.get("merged_at") or pr["base"]["ref"] != (await asyncio.to_thread(repo.snapshot))["branch"]:
                        from .. import automerge as am
                        held = next((x for x in reversed(store.interventions(run["id"])) if x["kind"] == am.HELD), None)
                        depth, _ = am.fix_depth(q["body"])
                        if held and cfg.auto_merge.auto_fix and depth < cfg.auto_merge.fix_attempts:
                            reason = json.loads(held["detail"] or "{}").get("reason", "PR held by merge policy")
                            title, body = am.fix_task(title=q["title"], body=q["body"], url=run["pr_url"], reason=reason, findings=[])
                            budget = min(q["budget"] or cfg.limits.budget_usd, p["budget_usd"] - ps.spent(pid))
                            ms = ps.milestones(pid)[p["current_milestone"]]
                            if budget <= 0 or ps.spent(pid, p["current_milestone"]) + budget > milestone_limit(cfg, p, ms):
                                stop(ps, p, "The held PR needs a fix-up, but its budget needs review first.", {"pr": run["pr_url"], "reason": reason})
                                return
                            with store.transaction():
                                store.conn.execute("UPDATE queue_items SET status='cancelled',project_processed=1 WHERE project_id=? AND status IN ('queued','tentative')", (pid,))
                                store.conn.execute("UPDATE queue_items SET project_processed=1 WHERE id=?", (q["id"],))
                                pos = store.conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM queue_items").fetchone()[0]
                                store.conn.execute("INSERT INTO queue_items(position,title,body,repo,created_at,status,queue,chain,approval,project_id,milestone,project_key,budget,mode,heal_attempts,seed_run_id) VALUES (?,?,?,?,?,'queued',?,0,'yes',?,?,?,?, 'swarm',0,?)",
                                                   (pos, title, body, p["repo"], time.time(), lane(pid), pid, p["current_milestone"], q["project_key"], budget, q["run_id"]))
                                ps.event(pid, "fix_up", {"supersedes": q["id"], "reason": reason})
                            return
                        ps.update(pid, last_error="Waiting for the previous task's PR to merge: " + run["pr_url"])
                        return
                    sha = pr["merge_commit_sha"]
                else:
                    ps.update(pid, last_error="Completed run has no merged PR. Review its run before continuing.")
                    return
                store.queue_update(q["id"], project_processed=1, merged_sha=sha)
                ps.event(pid, "merge" if sha else "spike_finished", {"task": q["id"], "sha": sha})
                store.kv_set("project_auto_context:" + pid, None)
                progress = True
            else:
                store.queue_update(q["id"], project_processed=1)
                if run and run.get("execution_id"):
                    recovery_lineages.add(run["execution_id"])
                if run and run.get("stop_kind"):
                    from ..execution import snapshot as execution_snapshot
                    allowance = execution_snapshot(store, run["id"])
                    exhausted_recovery = run["stop_kind"] in ("stalled", "lost_worker") and allowance and allowance["recoveries"] >= allowance["max_recoveries"]
                    if run["stop_kind"] in ("cancelled", "allowance_exhausted", "external_uncertain") or exhausted_recovery:
                        from .autonomy import recover_allowance
                        if recover_allowance(cfg, store, p, q, run):
                            return
                        stop(ps, p, f"Task stopped: {q['title']}. {run.get('state_reason') or run['stop_kind']}. Review its recovery options.",
                             {"task": q["project_key"], "run_id": run["id"], "stop_kind": run["stop_kind"], "reason": run.get("state_reason")})
                        return
                failures = sum(x["project_key"] == q["project_key"] and (x["status"] == "failed" or (x["status"] == "cancelled" and bool(x.get("run_id")))) for x in ps.items(pid))
                if failures >= cfg.projects.escalate_failures:
                    from .autonomy import schedule_repair
                    if schedule_repair(cfg, store, p, "task:" + q["project_key"], "Repeated task failure: revise or split this work using its retained code and test failures. Do not repeat the unchanged attempt."):
                        if run and run.get("execution_id"):
                            store.kv_set("project_recovery:" + pid, run["execution_id"])
                        return
                    stop(ps, p, "The same problem has failed three times. Propose a correction before resuming.", {"task": q["project_key"], "failures": failures})
                    return
                if failures < cfg.projects.max_task_failures:
                    from ..postmortem import resumable
                    seed = run["id"] if run and resumable(store, run["id"]) else q.get("seed_run_id")
                    store._exec("UPDATE queue_items SET status='cancelled',project_processed=1 WHERE project_id=? AND status IN ('queued','tentative')", (pid,))
                    diagnosis = str((run or {}).get("state_reason") or q.get("note") or "Inspect the retained run evidence")
                    store.queue_add(q["title"], q["body"] + "\nPrevious failure: " + diagnosis, p["repo"], project_id=pid, milestone=p["current_milestone"], project_key=q["project_key"], queue=lane(pid), chain=0, heal_attempts=0, budget=q["budget"], mode=q["mode"], seed_run_id=seed, execution_id=(run or {}).get("execution_id"), retry_of=(run or {}).get("id"), max_runtime_s=q.get("max_runtime_s"), max_cycles=q.get("max_cycles"), team=q.get("team"), depends_on=q.get("depends_on") or "[]")
                    from ..runqueue import set_lane_paused
                    set_lane_paused(store, lane(pid), False)
                    return
                progress = True
        spent = ps.spent(pid)
        if len(recovery_lineages) > 1:
            stop(ps, p, "The replacement plan spans multiple task allowances. Review its scope before continuing.",
                 {"execution_ids": sorted(recovery_lineages)})
            return
        recovery_lineage = next(iter(recovery_lineages), None)
        if recovery_lineage:
            allowance = store._one("SELECT deadline,budget_usd,spent_usd FROM execution_allowances WHERE id=?", (recovery_lineage,))
            if allowance and (time.time() >= allowance["deadline"] or allowance["spent_usd"] >= allowance["budget_usd"]):
                from .autonomy import recover_lineage
                if recover_lineage(cfg, store, p, recovery_lineage):
                    return
                stop(ps, p, "The task's shared allowance is exhausted. Review its scope and retained evidence before replanning.",
                     {"execution_id": recovery_lineage})
                return
        ms = ps.milestones(pid)[p["current_milestone"]]
        mspent = ps.spent(pid, p["current_milestone"])
        if cfg.projects.autonomous and mspent >= ms["estimate_usd"] * cfg.projects.escalate_budget_factor and spent < p["budget_usd"]:
            notice = f"project_estimate_reallocated:{pid}:{p['current_milestone']}:{ms['estimate_usd']}"
            if not store.kv_get(notice):
                store.kv_set(notice, "1")
                ps.event(pid, "budget_reallocated", {"milestone": p["current_milestone"], "estimate_usd": ms["estimate_usd"],
                                                     "milestone_spent_usd": mspent, "project_remaining_usd": p["budget_usd"] - spent})
        if spent >= p["budget_usd"] or mspent >= milestone_limit(cfg, p, ms):
            stop(ps, p, "Budget checkpoint: review spend and choose a revised plan before continuing.", {"spent_usd": spent, "milestone_spent_usd": mspent})
            return
        store.kv_set(activity_key, "Preparing the GitHub repository")
        await asyncio.to_thread(repo.ensure, p["definition"]["repository_kind"], pid)
        snapshot = await asyncio.to_thread(repo.snapshot)
        if any(q["status"] == "queued" and q.get("seed_run_id") for q in ps.items(pid)):
            ps.update(pid, observed_sha=snapshot["sha"])
            return  # A held PR's replacement must finish before the ordinary horizon is replanned.
        # Never start ready tasks planned against a different repository state.
        if p["observed_sha"] == snapshot["sha"] and not progress and any(q["status"] == "queued" for q in items):
            return
        recent = []
        for q in ps.items(pid)[-12:]:
            r = store.get_run(q["run_id"]) if q.get("run_id") else None
            report = ""
            if r and r.get("run_dir"):
                path = Path(r["run_dir"]) / "report.md"
                if path.exists():
                    report = path.read_text(encoding="utf-8", errors="replace")[-16000:]
            recent.append({"task": q, "run": r, "report": report})
        context = {"date": datetime.now(UTC).date().isoformat(), "spec": p["spec"], "repository": snapshot,
                   "milestone": p["current_milestone"], "recent_outcomes": recent,
                   "chat": store.conversation_messages(p["conversation_id"])[-12:], "budget_remaining": p["budget_usd"] - spent,
                   "verification": verify_checks(store, p)}
        landed = next((q for q in reversed(ps.items(pid)) if q["merged_sha"] and q.get("run_id")), None)
        if landed and hasattr(repo, "change"):
            run = store.get_run(landed["run_id"])
            if run and run.get("pr_url"):
                context["latest_diff"] = await asyncio.to_thread(repo.change, run["pr_url"])
        from .actions import ActionKind
        from .decisions import choose_action
        store.kv_set(activity_key, "Choosing the next bounded project action")
        selected = await choose_action(cfg, store, p, repo, snapshot, ps.items(pid), context, ask, transport,
                                       turn, validate_plan,
                                       lambda message, detail, **journal: stop(ps, ps.get(pid), message, detail, turn=turn if journal else None, **journal))
        if selected is None:
            return
        action, result, proposal = selected
        if action.kind == ActionKind.CONTINUE:
            from .actions import validate_scope
            from .decisions import queue_manifest
            from ..runqueue import set_lane_paused
            with store.transaction():
                current = ps.get(pid)
                rows = ps.items(pid)
                if current["status"] != "running" or current["spec"] != p["spec"] or queue_manifest(rows) != result["queue_manifest"]:
                    return
                validate_scope(action, current, rows, context["evidence_catalog"], max_runtime_s=cfg.limits.max_runtime_s,
                               investigation_timeout_s=cfg.projects.investigation_timeout_s)
                target = next(q for q in rows if q["id"] == action.task_id)
                reservation = target["budget"] or cfg.limits.budget_usd
                if ps.spent(pid) + reservation > p["budget_usd"] or ps.spent(pid, p["current_milestone"]) + reservation > milestone_limit(cfg, p, ms):
                    stop(ps, current, "The next existing task needs a budget review before continuing.", {"task_id": target["id"]})
                    return
                ps.update(pid, observed_sha=snapshot["sha"], last_error=None)
                ps.event(pid, "plan_continued", {"task_id": target["id"], "task": target["project_key"], "reason": action.reason,
                                               "explanation": result.get("normalization") or "Continuing the existing accepted task; scope, allowance and later tasks are unchanged."})
                turn.mark("applied", {"result": "Continued the accepted horizon without replacing tasks", "task_id": target["id"],
                                      "normalization": result.get("normalization")}, connection=store.conn)
                set_lane_paused(store, lane(pid), False)
                store.kv_set("project_auto_context:" + pid, None)
            return
        from ..execution import ensure_allowance
        for task in action.tasks:
            for target_id in task.replaces:
                target = next(q for q in ps.items(pid) if q["id"] == target_id)
                target_run = store.get_run(target["run_id"]) if target.get("run_id") else None
                inherited = target.get("execution_id") or (ensure_allowance(store, target_run["id"], cfg,
                            target_run.get("heal_of") or target_run.get("retry_of") or target_run.get("seed_run_id")) if target_run else None)
                if inherited:
                    recovery_lineages.add(inherited)
        if len(recovery_lineages) > 1:
            turn.mark("blocked", {"error": "Replacement targets span multiple task allowances"})
            stop(ps, p, "Replacement targets span multiple task allowances. Review the saved decision before continuing.", {"decision_id": turn.decision_id})
            return
        recovery_lineage = next(iter(recovery_lineages), None)
        if recovery_lineage:
            allowance = store._one("SELECT deadline,budget_usd,spent_usd,cancelled FROM execution_allowances WHERE id=?", (recovery_lineage,))
            if allowance and (allowance["cancelled"] or time.time() >= allowance["deadline"] or allowance["spent_usd"] >= allowance["budget_usd"]):
                turn.mark("blocked", {"error": "Replacement allowance is exhausted or intentionally cancelled"})
                stop(ps, p, "The replacement retains a stopped task allowance. Review it before continuing.", {"decision_id": turn.decision_id})
                return
        current = ps.get(pid)
        if current["status"] != "running" or current["spec"] != p["spec"]:
            return  # A pause, cancellation or human spec revision supersedes an in-flight proposal.
        spent = ps.spent(pid)
        mspent = ps.spent(pid, p["current_milestone"])
        if spent >= p["budget_usd"] or mspent >= milestone_limit(cfg, p, ms):
            stop(ps, p, "Lead planning reached the budget checkpoint. Review its spend before scheduling more work.", {"spent_usd": spent})
            return
        pending = {q["project_key"]: q for q in ps.items(pid) if q["status"] in ("queued", "tentative")}
        changed = [t["key"] for t in result.get("tasks", []) if t["key"] in pending and not pending[t["key"]]["body"].startswith(t["description"] + "\n")]
        counts = {}
        for event in reversed(ps.events(pid)):
            if event["kind"] == "merge":
                break
            if event["kind"] == "replan":
                for key in json.loads(event["detail"]).get("changed_keys", []):
                    counts[key] = counts.get(key, 0) + 1
        for key in changed:
            if counts.get(key, 0) >= 3:
                store.queue_update(pending[key]["id"], status="failed", project_processed=0, note="Plan churn: more than three rewrites without merged progress")
                ps.event(pid, "plan_churn_failure", {"key": key})
                return
        # Protect fixed bytes in repository too; persist the living spec before dispatch.
        store.kv_set(activity_key, "Saving the plan to the repository")
        turn.mark("executing", {"repository_sha": snapshot["sha"]})
        await asyncio.to_thread(repo.write_spec, p, proposal, snapshot)
        snapshot = await asyncio.to_thread(repo.snapshot)
        with store.transaction():
            latest = ps.get(pid)
            if latest["status"] != "running" or latest["spec"] != p["spec"]:
                return
            ps.update(pid, spec=proposal, observed_sha=snapshot["sha"], last_error=None, previous_spec=None)
        p = ps.get(pid)
        if result["action"] == "escalate":
            stop(ps, p, result["proposal"], result, turn=turn, decision_status="applied",
                 decision_result={"result": "Escalated for a human decision", "proposal": result["proposal"]})
            return
        if result["action"] == "checkpoint":
            if not any(q["merged_sha"] and q["milestone"] == p["current_milestone"] for q in ps.items(pid)):
                raise ProjectError("A milestone cannot finish without merged work")
            checkpoint(store, p, result["reason"], config=cfg)
            turn.mark("applied", {"result": "Milestone checks evaluated", "status": ps.get(pid)["status"]})
            return
        ready = [t for t in result["tasks"] if t.get("ready")]
        next_budget = ready[0]["budget_usd"]
        if spent + next_budget > p["budget_usd"] or mspent + next_budget > milestone_limit(cfg, p, ms):
            stop(ps, p, "The next task would exceed the approved project or milestone budget. Review its scope or budget.", {"next_task_budget": next_budget, "spent": spent})
            return
        # Retain completed/failed history; replace only the pending horizon, atomically.
        from ..runqueue import set_lane_paused
        with store.transaction():
            latest = ps.get(pid)
            if latest["status"] != "running" or latest["spec"] != proposal:
                return
            store.conn.execute("UPDATE queue_items SET status='cancelled',project_processed=1 WHERE project_id=? AND status IN ('queued','tentative')", (pid,))
            pos = store.conn.execute("SELECT COALESCE(MAX(position),0) FROM queue_items").fetchone()[0]
            for i, t in enumerate(result["tasks"]):
                key = ("spike:" if t["kind"] == "spike" else "") + t["key"].removeprefix("spike:")
                from ..criteria import split as split_criteria
                gated, person = split_criteria(t.get("acceptance_criteria", []))
                body = (t["description"] + "\n\nAcceptance criteria:\n" + "\n".join(gated)
                        + ("\n\nFor a person to check after merge (not acceptance criteria; never block this run):\n"
                           + "\n".join(person) if person else "")
                        + "\nLikely files: " + ", ".join(t.get("files", [])) + "\n\nApproved project spec (fixed part must be unchanged):\n" + proposal)
                status = "queued" if t.get("ready") else "tentative"
                store.conn.execute("INSERT INTO queue_items(position,title,body,repo,created_at,status,queue,chain,approval,project_id,milestone,project_key,budget,mode,heal_attempts,execution_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (pos + i + 1, t["title"], body, p["repo"], time.time(), status, lane(pid), 0, "yes", pid, p["current_milestone"], key, t["budget_usd"], "single" if t["kind"] == "spike" else "reviewed" if action.kind == ActionKind.REVIEW else "swarm", 0, recovery_lineage))
                qid = store.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                if recovery_lineage and t.get("ready") and t["kind"] == "build":
                    from ..postmortem import resumable
                    candidates = store._all("SELECT q.run_id FROM queue_items q JOIN runs r ON r.id=q.run_id WHERE q.project_id=? "
                                            "AND q.milestone=? AND q.project_key=? AND r.execution_id=? AND q.status='failed' ORDER BY q.id DESC",
                                            (pid, p["current_milestone"], key, recovery_lineage))
                    seed = next((old["run_id"] for old in candidates if resumable(store, old["run_id"])), None)
                    if seed:
                        store.conn.execute("UPDATE queue_items SET seed_run_id=?,retry_of=? WHERE id=?", (seed, seed, qid))
                runtime = t.get("max_runtime_s") or (min(cfg.projects.investigation_timeout_s, cfg.limits.max_runtime_s) if t["kind"] == "spike" else cfg.limits.max_runtime_s)
                store.conn.execute("UPDATE queue_items SET depends_on=?,project_action=?,max_runtime_s=? WHERE id=?",
                                   (json.dumps(t.get("depends_on", [])), action.kind.value, runtime, qid))
            store.conn.execute("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)", (pid, time.time(), "replan", json.dumps({"sha": snapshot["sha"], "reason": result["reason"], "tasks": result["tasks"], "changed_keys": changed})))
            turn.mark("applied", {"result": "Scheduled approved tasks in the existing project lane", "task_keys": [t["key"] for t in result["tasks"]]}, connection=store.conn)
            set_lane_paused(store, lane(pid), False)
            store.kv_set("project_recovery:" + pid, None)
            store.kv_set("project_auto_context:" + pid, None)
    finally:
        from .autonomy import instructions
        recovery = instructions(store, pid) if cfg.projects.autonomous else {}
        descriptions = {"planning": "Repairing the project plan", "task_repair": "Replanning failed work", "milestone_verification": "Checking missing milestone evidence", "environment": "Waiting to recheck the worker environment", "clarification": "Trying an alternative before asking you"}
        activity = "Recovering automatically: " + descriptions.get(recovery.get("problem"), "trying a revised approach") if recovery and ps.get(pid)["status"] == "running" else None
        store.kv_set(activity_key, activity)
        if own:
            repo.close()


def stop(ps, p, proposal, detail, *, turn=None, decision_status=None, decision_result=None):
    from ..execution import transaction
    now = time.time()
    with transaction(ps.store) as db:
        current = db.execute("SELECT status FROM projects WHERE id=?", (p["id"],)).fetchone()
        if not current or current["status"] != "running":
            if turn:
                turn.mark("superseded", {"reason": "Project state changed before applying the decision"}, connection=db)
            return
        db.execute("UPDATE projects SET status='waiting_for_you',last_error=? WHERE id=?", (proposal, p["id"]))
        db.execute("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)", (p["id"], now, "escalation", json.dumps(detail)))
        db.execute("INSERT INTO chat_messages(conversation_id,role,content,created_at) VALUES (?,'assistant',?,?)", (p["conversation_id"], proposal, now))
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, p["conversation_id"]))
        if turn and decision_status:
            turn.mark(decision_status, decision_result, connection=db)


async def tick(cfg, store, **kw):
    slots = asyncio.Semaphore(max(1, cfg.limits.max_parallel_queues))

    async def one(p):
        try:
            async with slots:
                from .autonomy import remember_policy, recover_waiting, recover_environment
                remember_policy(cfg, store, p["id"])
                if p["status"] != "running":
                    if not recover_waiting(cfg, store, p):
                        return
                recover_environment(cfg, store, Projects(store).get(p["id"]))
                await advance(cfg, store, p["id"], **kw)
        except Exception as e:  # One project must never stop the worker or another lane.
            ps = Projects(store)
            if ps.get(p["id"])["status"] == "running":
                stop(ps, ps.get(p["id"]), f"Project service needs attention: {type(e).__name__}: {e}", {"error": str(e)})
    await asyncio.gather(*(one(p) for p in Projects(store).list()))
