"""Durable project state machine. Only this layer applies lead proposals."""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from . import lead
from .repository import Repository
from .spec import ProjectError, fixed, replace_living, validate
from .store import Projects


def lane(pid):
    return "project-" + pid


def approve(store, pid):
    ps = Projects(store)
    with store._lock, store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        p = ps.get(pid)
        if p["status"] != "drafting":
            raise ProjectError("This project has already been launched")
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
    p = ps.get(pid)
    if p["status"] != "running":
        raise ProjectError("Only a running project can be paused")
    ps.update(pid, status="waiting_for_you", last_error="Paused by you. Running work will finish; no new task will start.")
    ps.event(pid, "pause", {})


def cancel(store, pid):
    ps = Projects(store)
    p = ps.get(pid)
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
    with store._lock, store.conn:
        store.conn.execute("UPDATE projects SET name=?,budget_usd=?,definition=?,spec=?,previous_spec=?,observed_sha=NULL WHERE id=?",
                           (d["name"], d["budget_usd"], json.dumps(d), render(d, living), p.get("previous_spec") or p["spec"], pid))
        for i, m in enumerate(d["milestones"]):
            if i >= p["current_milestone"]:
                store.conn.execute("UPDATE milestones SET name=?,estimate_usd=?,estimate_hours=?,report=NULL,status=? WHERE project_id=? AND idx=?",
                                   (m["name"], m["estimate_usd"], m["estimate_hours"], "current" if i == p["current_milestone"] else "ahead", pid, i))
        store.conn.execute("UPDATE projects SET status='waiting_for_you' WHERE id=?", (pid,))
        store.conn.execute("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)", (pid, time.time(), "spec_approved", json.dumps({"before": p["spec"], "after": render(d, living)})))


def resume(store, pid, reply="continue", *, confirm_manual=False):
    ps = Projects(store)
    if not reply.strip():
        raise ProjectError("Add a reply or correction")
    with store._lock, store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        p = ps.get(pid)
        if p["status"] not in ("at_checkpoint", "waiting_for_you"):
            raise ProjectError("Project is not waiting for a reply")
        status, index = "running", p["current_milestone"]
        now = time.time()
        if p["status"] == "at_checkpoint" and reply.strip().lower() in ("continue", "approved", "looks good"):
            milestones = ps.milestones(pid)
            report = json.loads(milestones[index]["report"])
            if any(c["status"] == "needs you" for c in report["checks"]) and not confirm_manual:
                raise ProjectError("Confirm the checks marked needs you before continuing")
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
    from ..runqueue import set_lane_paused
    set_lane_paused(store, lane(pid), False)


def can_start(store, item):
    if not item.get("project_id"):
        # Single tasks in a project's repository wait; other repositories are unaffected.
        from ..repos import normalize_repo
        try:
            name = normalize_repo(item["repo"]).full_name
        except ValueError:
            name = None
        return not name or not any(p["repo"].lower() == name.lower() and p["status"] not in ("drafting", "done", "cancelled") for p in Projects(store).list())
    ps = Projects(store)
    p = ps.get(item["project_id"])
    if p["status"] != "running" or not p["observed_sha"] or item["milestone"] != p["current_milestone"]:
        return False
    queued = [q for q in ps.items(p["id"]) if q["status"] == "queued" and q["milestone"] == p["current_milestone"]]
    if item.get("queue") != lane(p["id"]) or not queued or queued[0]["id"] != item["id"]:
        return False
    return not any(q["id"] != item["id"] and (q["status"] == "running" or (q["status"] in ("done", "failed", "cancelled") and not q["project_processed"])) for q in ps.items(p["id"]))


def validate_plan(p, result, horizon):
    action = result.get("action")
    if action not in ("plan", "checkpoint", "escalate") or not isinstance(result.get("reason"), str) or not result["reason"].strip():
        raise ProjectError("Lead proposal requires a known action and reason")
    living = result.get("living")
    if not isinstance(living, str) or any("### " + h not in living for h in ("Current state", "Decisions made", "Plan", "Plan changes", "Open questions")):
        raise ProjectError("Lead must supply all living sections")
    spec = fixed(p["spec"]) + "## Living\n\n" + living
    replace_living(p["spec"], spec)
    for heading in ("Decisions made", "Plan changes"):
        pattern = rf"(?ms)^### {heading}\n(.*?)(?=^### |\Z)"
        old = re.search(pattern, p["spec"])
        new = re.search(pattern, living)
        if old and old.group(1).strip() and (not new or old.group(1).strip() not in new.group(1)):
            raise ProjectError(f"Preserve the existing {heading.lower()} history and append new entries")
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


def checkpoint(store, p, reason):
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
    store._exec("UPDATE milestones SET status='checkpoint',report=?,spent_usd=? WHERE project_id=? AND idx=?",
                (json.dumps(report), report["spent_usd"], p["id"], p["current_milestone"]))
    ps.update(p["id"], status="at_checkpoint")
    ps.event(p["id"], "checkpoint", report)
    store.conversation_append(p["conversation_id"], "assistant", "Milestone checkpoint\n\n" + reason + "\n\n" + "\n".join(f"- {c['status']}: {c['text']}" for c in checks) + "\n\nReview the report and reply continue, or describe a correction.")


async def advance(cfg, store, pid, *, repository=None, ask=None, transport=None):
    ps = Projects(store)
    p = ps.get(pid)
    if p["status"] != "running":
        return
    own = repository is None
    repo = repository or Repository(cfg, p["repo"])
    ask = ask or lead.call
    activity_key = f"project_activity:{pid}"
    try:
        items = ps.items(pid)
        if any(q["status"] == "running" for q in items):
            return
        progress = False
        for q in items:
            if q["project_processed"] or q["status"] not in ("done", "failed", "cancelled"):
                continue
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
                            if budget <= 0 or ps.spent(pid, p["current_milestone"]) + budget > ms["estimate_usd"] * cfg.projects.escalate_budget_factor:
                                stop(ps, p, "The held PR needs a fix-up, but its budget needs review first.", {"pr": run["pr_url"], "reason": reason})
                                return
                            with store._lock, store.conn:
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
                progress = True
            else:
                store.queue_update(q["id"], project_processed=1)
                failures = sum(x["project_key"] == q["project_key"] and (x["status"] == "failed" or (x["status"] == "cancelled" and bool(x.get("run_id")))) for x in ps.items(pid))
                if failures >= cfg.projects.escalate_failures:
                    stop(ps, p, "The same problem has failed three times. Propose a correction before resuming.", {"task": q["project_key"], "failures": failures})
                    return
                if failures < cfg.projects.max_task_failures:
                    store._exec("UPDATE queue_items SET status='cancelled',project_processed=1 WHERE project_id=? AND status IN ('queued','tentative')", (pid,))
                    store.queue_add(q["title"], q["body"] + "\nPrevious failure: " + str(q.get("note")), p["repo"], project_id=pid, milestone=p["current_milestone"], project_key=q["project_key"], queue=lane(pid), chain=0, heal_attempts=0, budget=q["budget"], mode=q["mode"], seed_run_id=q.get("seed_run_id"))
                    from ..runqueue import set_lane_paused
                    set_lane_paused(store, lane(pid), False)
                    return
                progress = True
        spent = ps.spent(pid)
        ms = ps.milestones(pid)[p["current_milestone"]]
        mspent = ps.spent(pid, p["current_milestone"])
        if spent >= p["budget_usd"] or mspent >= ms["estimate_usd"] * cfg.projects.escalate_budget_factor:
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
        proposal = None
        for attempt in range(2):
            store.kv_set(activity_key, "Planning the next tasks" if p["observed_sha"] else "Planning the first tasks")
            result = await ask(cfg, store, p, lead.LEAD, context, transport)
            try:
                proposal = validate_plan(p, result, cfg.projects.horizon)
                if result["action"] == "checkpoint" and any(c["status"] == "failed" for c in context["verification"]):
                    raise ProjectError("Milestone checks failed; schedule repairs instead of declaring completion")
                break
            except ProjectError as e:
                ps.event(pid, "proposal_rejected", {"reason": str(e)})
                context["rejected_proposal"] = result
                context["correction_required"] = str(e)
        if proposal is None:
            raise ProjectError("Lead returned two invalid proposals. Retry after correcting the model configuration.")
        current = ps.get(pid)
        if current["status"] != "running" or current["spec"] != p["spec"]:
            return  # A pause, cancellation or human spec revision supersedes an in-flight proposal.
        spent = ps.spent(pid)
        mspent = ps.spent(pid, p["current_milestone"])
        if spent >= p["budget_usd"] or mspent >= ms["estimate_usd"] * cfg.projects.escalate_budget_factor:
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
        await asyncio.to_thread(repo.write_spec, p, proposal, snapshot)
        snapshot = await asyncio.to_thread(repo.snapshot)
        if ps.get(pid)["status"] != "running":
            return
        ps.update(pid, spec=proposal, observed_sha=snapshot["sha"], last_error=None, previous_spec=None)
        p = ps.get(pid)
        if result["action"] == "escalate":
            stop(ps, p, result["proposal"], result)
            return
        if result["action"] == "checkpoint":
            if not any(q["merged_sha"] and q["milestone"] == p["current_milestone"] for q in ps.items(pid)):
                raise ProjectError("A milestone cannot finish without merged work")
            checkpoint(store, p, result["reason"])
            return
        ready = [t for t in result["tasks"] if t.get("ready")]
        next_budget = ready[0]["budget_usd"]
        if spent + next_budget > p["budget_usd"] or mspent + next_budget > ms["estimate_usd"] * cfg.projects.escalate_budget_factor:
            stop(ps, p, "The next task would exceed the approved project or milestone budget. Review its scope or budget.", {"next_task_budget": next_budget, "spent": spent})
            return
        # Retain completed/failed history; replace only the pending horizon, atomically.
        with store._lock, store.conn:
            store.conn.execute("UPDATE queue_items SET status='cancelled',project_processed=1 WHERE project_id=? AND status IN ('queued','tentative')", (pid,))
            pos = store.conn.execute("SELECT COALESCE(MAX(position),0) FROM queue_items").fetchone()[0]
            for i, t in enumerate(result["tasks"]):
                key = ("spike:" if t["kind"] == "spike" else "") + t["key"].removeprefix("spike:")
                body = t["description"] + "\n\nAcceptance criteria:\n" + "\n".join(t.get("acceptance_criteria", [])) + "\nLikely files: " + ", ".join(t.get("files", [])) + "\n\nApproved project spec (fixed part must be unchanged):\n" + proposal
                status = "queued" if t.get("ready") else "tentative"
                store.conn.execute("INSERT INTO queue_items(position,title,body,repo,created_at,status,queue,chain,approval,project_id,milestone,project_key,budget,mode,heal_attempts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (pos + i + 1, t["title"], body, p["repo"], time.time(), status, lane(pid), 0, "yes", pid, p["current_milestone"], key, t["budget_usd"], "single" if t["kind"] == "spike" else "swarm", 0))
            store.conn.execute("INSERT INTO project_events(project_id,ts,kind,detail) VALUES (?,?,?,?)", (pid, time.time(), "replan", json.dumps({"sha": snapshot["sha"], "reason": result["reason"], "tasks": result["tasks"], "changed_keys": changed})))
        from ..runqueue import set_lane_paused
        set_lane_paused(store, lane(pid), False)
    finally:
        store.kv_set(activity_key, None)
        if own:
            repo.close()


def stop(ps, p, proposal, detail):
    ps.update(p["id"], status="waiting_for_you", last_error=proposal)
    ps.event(p["id"], "escalation", detail)
    ps.store.conversation_append(p["conversation_id"], "assistant", proposal)


async def tick(cfg, store, **kw):
    slots = asyncio.Semaphore(max(1, cfg.limits.max_parallel_queues))

    async def one(p):
        if p["status"] != "running":
            return
        try:
            async with slots:
                await advance(cfg, store, p["id"], **kw)
        except Exception as e:  # One project must never stop the worker or another lane.
            ps = Projects(store)
            ps.update(p["id"], status="waiting_for_you", last_error=f"Project service needs attention: {type(e).__name__}: {e}")
            ps.event(p["id"], "service_error", {"error": str(e)})
    await asyncio.gather(*(one(p) for p in Projects(store).list()))
