"""Speed metrics: where each run's time went, so settings changes can be measured instead of guessed.

Everything is computed from what runs already record (state events, agent tasks, model calls, test runs,
approvals), so it works for old runs too. Each finished run also gets a `metrics.json` in its folder.

* `run_metrics(store, run_id)`   one run: time per phase, waits, agents, models, tests, settings
* `collect(store, limit)`        recent finished runs, as flat rows (dashboard table / CSV)
* `agent_rows(store, limit)`     one row per agent session (compare models and roles)
* `summarize(rows)`              medians, and the same grouped by settings, to compare before/after
"""
from __future__ import annotations

import csv
import io
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}
# run states -> the phase they count towards ("where the time went")
PHASE_OF = {
    "ISSUE_RECEIVED": "setup", "REPOSITORY_INSPECTION": "setup",
    "PLANNING": "planning", "TASK_VALIDATION": "planning",
    "TASK_EXECUTION": "coding", "INTEGRATION": "coding",
    "TESTING": "testing",
    "CODE_REVIEW": "review", "FIX_REQUIRED": "review",
    "APPROVED": "delivery", "PULL_REQUEST_CREATION": "delivery",
}
PHASES = ["waiting", "setup", "planning", "coding", "testing", "review", "delivery"]
PHASE_HELP = {
    "waiting": "in line for the repository (another run was working on it)",
    "setup": "clone, dependencies and the baseline test run",
    "planning": "the planner splitting the work into tasks",
    "coding": "developer agents writing code, then merging their branches",
    "testing": "the tester agent writing acceptance tests, plus the test-suite runs",
    "review": "the reviewer reading the change, and deciding on repairs",
    "delivery": "pushing and opening the PR, including waiting for your approval",
}


def _short_model(m: str | None) -> str:
    return (m or "").split("/")[-1]


def settings_label(config_json: str | None) -> str:
    """A compact fingerprint of the settings that drive speed, e.g. to compare before/after a change."""
    try:
        c = json.loads(config_json or "{}")
    except json.JSONDecodeError:
        return "?"
    roles, lim = c.get("roles") or {}, c.get("limits") or {}
    parts = []
    for role in [*(c.get("developer_roles") or []), "reviewer", "tester"]:
        r = roles.get(role) or {}
        if r:
            short = {"developer_backend": "be", "developer_frontend": "fe"}.get(role, role[:3])
            parts.append(f"{short}={_short_model(r.get('model'))}/{r.get('max_steps')}")
    parts.append(f"cycles={lim.get('max_repair_cycles')}")
    block = (c.get("review") or {}).get("blocking_severities")
    if block:
        parts.append("block=" + "+".join(block))
    if "tester_every_cycle" in lim:
        parts.append("tester=" + ("every" if lim["tester_every_cycle"] else "once"))
    return " ".join(parts)


def run_metrics(store, run_id: str) -> dict | None:
    run = store.get_run(run_id)
    if not run:
        return None
    end = run.get("finished_at") or run.get("updated_at") or run["created_at"]
    events = store._all("SELECT ts, kind, level, message, data FROM events WHERE run_id = ? ORDER BY id", (run_id,))

    # ---- phases: time spent in each state, folded into PHASES
    states, wait_s = [], 0.0
    for e in events:
        if e["kind"] == "state":
            try:
                states.append((e["ts"], json.loads(e["data"] or "{}").get("to")))
            except json.JSONDecodeError:
                pass
        elif e["kind"] == "queue" and "repository is free after" in (e["message"] or ""):
            wait_s = max(wait_s, e["ts"] - run["created_at"])
    phases = dict.fromkeys(PHASES, 0.0)
    t, cur = run["created_at"], "ISSUE_RECEIVED"
    for ts, to in states + [(end, None)]:
        phases[PHASE_OF.get(cur, "setup")] += max(0.0, ts - t)
        t, cur = ts, (to or cur).split(":")[0]
        if cur in TERMINAL:
            break
    wait_s = min(wait_s, phases["setup"])        # waiting happens before the first state change
    phases["waiting"], phases["setup"] = wait_s, phases["setup"] - wait_s

    # ---- approvals, tests, agents, models
    approval_s = sum((a["decided_at"] - a["ts"]) for a in store._all(
        "SELECT ts, decided_at FROM approvals WHERE run_id = ? AND decided_at IS NOT NULL", (run_id,)))
    tests = store._all("SELECT label, duration_s, timed_out FROM test_runs WHERE run_id = ?", (run_id,))
    tasks = store.tasks(run_id)
    calls = store._all("SELECT role, model, task_key, started_at, ended_at, total_tokens, cost_usd, status "
                       "FROM llm_calls WHERE run_id = ?", (run_id,))
    roles: dict[str, dict] = defaultdict(lambda: {"agents": 0, "agent_s": 0.0, "calls": 0, "call_s": 0.0,
                                                  "tokens": 0, "cost_usd": 0.0, "retries": 0})
    for tk in tasks:
        r = roles[tk.get("role") or "?"]
        r["agents"] += 1
        if tk.get("started_at") and tk.get("ended_at"):
            r["agent_s"] += tk["ended_at"] - tk["started_at"]
        r["retries"] += max(0, (tk.get("attempts") or 1) - 1)
    for c in calls:
        r = roles[c.get("role") or "?"]
        r["calls"] += 1
        if c.get("started_at") and c.get("ended_at"):
            r["call_s"] += c["ended_at"] - c["started_at"]
        r["tokens"] += c.get("total_tokens") or 0
        r["cost_usd"] += c.get("cost_usd") or 0.0
    for r in roles.values():   # planner/reviewer run without a task row: their busy time is their call time
        r["time_s"] = r["agent_s"] or r["call_s"]
    msgs = [e["message"] or "" for e in events]
    counts = {
        "agent_timeouts": sum(1 for m in msgs if m.startswith("end: timeout")),
        "step_limit_hits": sum(1 for m in msgs if "hitting its limit" in m),
        "model_busy_waits": sum(1 for m in msgs if "is busy (429" in m or "is busy (503" in m),
        # SwarmForge tools used by Pi developers through `sf` (each call is logged as "sf <tool>: ...")
        "sf_calls": sum(1 for m in msgs if re.match(r"sf \w+: ", m)),
        "browse_calls": sum(1 for m in msgs if m.startswith(("sf browse: ", "browse: "))),
    }
    total = end - run["created_at"]
    return {
        "run_id": run_id, "title": run.get("issue_title") or "", "state": run["state"],
        "started": run["created_at"], "total_s": total, "work_s": total - phases["waiting"],
        "repair_cycles": run.get("repair_cycle") or 0, "cost_usd": sum(c.get("cost_usd") or 0 for c in calls),
        "phases_s": phases, "approval_wait_s": approval_s,
        "test_runs": len(tests), "test_suite_s": sum(x.get("duration_s") or 0 for x in tests),
        "test_timeouts": sum(1 for x in tests if x.get("timed_out")),
        "roles": {k: dict(v) for k, v in roles.items()}, **counts,
        "settings": settings_label(run.get("config_json")),
    }


def _finished_runs(store, limit: int) -> list[str]:
    return [r["id"] for r in store._all(
        "SELECT id FROM runs WHERE finished_at IS NOT NULL AND state IN ('COMPLETED','FAILED','BUDGET_EXCEEDED') "
        "AND COALESCE(archived, 0) = 0 ORDER BY created_at DESC LIMIT ?", (limit,))]


def flat(m: dict) -> dict:
    """One flat row per run (minutes), for tables and CSV."""
    row = {"run_id": m["run_id"], "title": m["title"], "state": m["state"], "settings": m["settings"],
           "total_min": m["total_s"] / 60, "work_min": m["work_s"] / 60}
    row.update({f"{p}_min": m["phases_s"][p] / 60 for p in PHASES})
    row.update(approval_wait_min=m["approval_wait_s"] / 60, test_suite_min=m["test_suite_s"] / 60,
               test_runs=m["test_runs"], repair_cycles=m["repair_cycles"], cost_usd=m["cost_usd"],
               agent_timeouts=m["agent_timeouts"], step_limit_hits=m["step_limit_hits"],
               model_busy_waits=m["model_busy_waits"], sf_calls=m["sf_calls"], browse_calls=m["browse_calls"])
    for role, r in sorted(m["roles"].items()):
        row[f"{role}_min"] = r["time_s"] / 60
    row["started"] = m["started"]
    return row


def collect(store, limit: int = 50) -> list[dict]:
    out = []
    for rid in _finished_runs(store, limit):
        try:
            m = run_metrics(store, rid)
        except Exception:  # noqa: BLE001 - one odd run must not hide the rest
            continue
        if m and m["total_s"] >= 60:            # runs that died at once say nothing about speed
            out.append(flat(m))
    return out


def agent_rows(store, limit: int = 50) -> list[dict]:
    """One row per agent session: role, model, minutes, calls, seconds per call, retries, outcome."""
    ids = _finished_runs(store, limit)
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    calls = Counter()
    call_s: dict[tuple, float] = defaultdict(float)
    for c in store._all(f"SELECT run_id, task_key, started_at, ended_at FROM llm_calls WHERE run_id IN ({q})", tuple(ids)):
        k = (c["run_id"], c["task_key"])
        calls[k] += 1
        if c["started_at"] and c["ended_at"]:
            call_s[k] += c["ended_at"] - c["started_at"]
    out = []
    for t in store._all(f"SELECT * FROM tasks WHERE run_id IN ({q})", tuple(ids)):
        if not (t.get("started_at") and t.get("ended_at")):
            continue
        k = (t["run_id"], t["task_key"])
        out.append({"run_id": t["run_id"], "task": t["task_key"], "role": t["role"], "model": _short_model(t["model"]),
                    "cycle": t["cycle"], "minutes": (t["ended_at"] - t["started_at"]) / 60,
                    "calls": calls[k], "sec_per_call": (call_s[k] / calls[k]) if calls[k] else None,
                    "retries": max(0, (t.get("attempts") or 1) - 1), "state": t["state"]})
    return out


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def summarize(rows: list[dict]) -> dict:
    """Medians over the runs, and the same grouped by settings fingerprint."""
    def med(rs):
        keys = ["total_min", "work_min", *[f"{p}_min" for p in PHASES], "repair_cycles", "cost_usd"]
        return {"runs": len(rs), **{k: _median([r.get(k) for r in rs]) for k in keys}}
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r["settings"]].append(r)
    return {"all": med(rows) if rows else {"runs": 0}, "by_settings": {k: med(v) for k, v in groups.items()}}


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    cols: list[str] = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()})
    return buf.getvalue()


def write_run_metrics(store, run_id: str, run_dir: Path) -> Path | None:
    m = run_metrics(store, run_id)
    if not m:
        return None
    path = Path(run_dir) / "metrics.json"
    path.write_text(json.dumps(m, indent=2, default=str), encoding="utf-8")
    return path


def format_run(m: dict) -> str:
    """A short text breakdown (CLI / logs)."""
    total = m["total_s"] or 1
    lines = [f"{m['run_id']}  {m['state']}  {m['total_s'] / 60:.0f} min  ${m['cost_usd']:.2f}  "
             f"repair cycles {m['repair_cycles']}  |  {m['title'][:60]}"]
    for p in PHASES:
        s = m["phases_s"][p]
        if s >= 30:
            lines.append(f"  {p:<9} {s / 60:6.1f} min  {100 * s / total:4.0f}%")
    lines.append(f"  test suite {m['test_suite_s'] / 60:.1f} min over {m['test_runs']} run(s); approval wait "
                 f"{m['approval_wait_s'] / 60:.1f} min; agent timeouts {m['agent_timeouts']}, step-limit hits "
                 f"{m['step_limit_hits']}, busy-model waits {m['model_busy_waits']}; sf tool calls {m['sf_calls']} "
                 f"(browser {m['browse_calls']})")
    for role, r in sorted(m["roles"].items(), key=lambda kv: -kv[1]["time_s"]):
        if r["time_s"] or r["calls"]:
            per = f", {r['call_s'] / r['calls']:.0f}s/call" if r["calls"] else ""
            sess = f" in {r['agents']} session(s)" if r["agents"] else ""
            lines.append(f"  {role:<20} {r['time_s'] / 60:6.1f} min{sess}, {r['calls']} call(s){per}")
    lines.append(f"  settings: {m['settings']}")
    return "\n".join(lines)
