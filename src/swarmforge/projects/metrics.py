"""Project comparisons from retained attempts; completion and delivery remain distinct."""
from __future__ import annotations

import time
from collections import Counter, defaultdict

from ..metrics import PHASES, run_metrics
from .spec import execution_policy
from .store import Projects


def project_metrics(store, pid: str) -> dict:
    ps = Projects(store)
    project = ps.get(pid)
    items = ps.items(pid)
    runs = [store.get_run(rid) for rid in sorted(ps.run_ids(pid))]
    runs = [r for r in runs if r]
    workflows = {}
    phases = dict.fromkeys(PHASES, 0.0)
    acceptance = Counter()
    states = Counter(r["state"] for r in runs)
    tokens = calls = unpriced = repairs = 0
    validation_cost = 0.0
    approvals = 0
    approval_wait = 0.0
    models = defaultdict(lambda: {"calls": 0, "tokens": 0, "cost_usd": 0.0})
    for run in runs:
        m = run_metrics(store, run["id"])
        for phase, seconds in m["phases_s"].items():
            phases[phase] += seconds
        repairs += m["repair_cycles"]
        approval_wait += m["approval_wait_s"]
        approvals += store._one("SELECT COUNT(*) AS n FROM approvals WHERE run_id=? AND decided_at IS NOT NULL", (run["id"],))["n"]
        for row in store._all("SELECT status FROM acceptance WHERE run_id=? AND cycle=?", (run["id"], run.get("repair_cycle") or 0)):
            acceptance[row["status"]] += 1
        validation = m["validation"]
        if run.get("mode") == "single":
            validation = "baseline/spike"
        key = f"{run.get('mode') or 'swarm'} / {validation}"
        group = workflows.setdefault(key, {"workflow": key, "runs": 0, "completed": 0, "cost_usd": 0.0, "run_s": 0.0, "repair_cycles": 0})
        group["runs"] += 1
        group["completed"] += run["state"] == "COMPLETED"
        group["cost_usd"] += m["cost_usd"]
        group["run_s"] += m["total_s"]
        group["repair_cycles"] += m["repair_cycles"]
    # Include kickoff, lead and every retained execution attempt exactly once.
    for rid in ["project:" + pid, *[r["id"] for r in runs]]:
        for call in store._all("SELECT role,model,task_key,total_tokens,cost_usd FROM llm_calls WHERE run_id=?", (rid,)):
            calls += 1
            tokens += call["total_tokens"] or 0
            unpriced += call["cost_usd"] is None
            model = models[call["model"] or "unknown"]
            model["calls"] += 1
            model["tokens"] += call["total_tokens"] or 0
            model["cost_usd"] += call["cost_usd"] or 0
            if call["role"] in ("tester", "reviewer", "designer") or call["task_key"] == "merge-judge":
                validation_cost += call["cost_usd"] or 0
    merged = [q for q in items if q.get("merged_sha")]
    attempted_tasks = {(q["milestone"], q["project_key"]) for q in items if q.get("run_id") and not str(q.get("project_key") or "").startswith("spike:")}
    delivered_tasks = {(q["milestone"], q["project_key"]) for q in merged}
    problems = set()
    for q in merged:
        # A replacement or continuation retains all failures, even if its latest run passed.
        related = [old for old in items if old["milestone"] == q["milestone"] and old["project_key"] == q["project_key"]]
        if any(old["status"] == "failed" for old in related):
            problems.add(q["id"])
        related_ids = {old["run_id"] for old in related if old.get("run_id")}
        lineages = {r.get("execution_id") for r in runs if r["id"] in related_ids} - {None}
        if any((r["id"] in related_ids or r.get("execution_id") in lineages)
               and (r["state"] != "COMPLETED" or (r.get("repair_cycle") or 0) > 0) for r in runs):
            problems.add(q["id"])
        if any(any((t.get("attempts") or 0) > 1 for t in store.tasks(r["id"])) for r in runs
               if r["id"] in related_ids or r.get("execution_id") in lineages):
            problems.add(q["id"])
    cost = ps.spent(pid)
    events = ps.events(pid)
    event_counts = Counter(e["kind"] for e in events)
    owner_actions = sum(event_counts[k] for k in ("approval", "spec_approved", "reply", "completed", "pause", "cancel", "planning_retry", "task_allowance_extended"))
    terminal_events = [e["ts"] for e in events if e["kind"] in ("completed", "cancel")]
    end = terminal_events[-1] if terminal_events and project["status"] in ("done", "cancelled") else time.time()
    return {
        "execution": execution_policy(project["definition"]), "runs": len(runs), "run_states": dict(states),
        "merged_tasks": len(merged), "first_pass_merged_tasks": len(merged) - len(problems),
        "attempted_tasks": len(attempted_tasks), "delivery_rate": len(delivered_tasks) / len(attempted_tasks) if attempted_tasks else None,
        "first_pass_delivery_rate": (len(merged) - len(problems)) / len(merged) if merged else None,
        "cost_usd": cost, "cost_per_merged_task_usd": cost / len(merged) if merged else None,
        "lead_cost_usd": store.cost_summary("project:" + pid)["total"],
        "independent_validation_cost_usd": validation_cost, "unpriced_calls": unpriced,
        "model_calls": calls, "tokens": tokens, "repair_cycles": repairs,
        "elapsed_s": max(0, end - (project.get("approved_at") or project["created_at"])),
        "run_phase_s": phases, "approval_decisions": approvals, "approval_wait_s": approval_wait,
        "owner_actions": owner_actions, "automatic_recoveries": event_counts["automatic_recovery"],
        "acceptance_observations": dict(acceptance), "workflows": list(workflows.values()), "models": dict(models),
    }
